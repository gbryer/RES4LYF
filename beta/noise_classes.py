import torch
import torch.nn.functional as F

from torch               import nn, Tensor, Generator, lerp
from torch.nn.functional import unfold
from torch.distributions import StudentT, Laplace

import numpy as np
import pywt
import functools

from typing import Callable, Tuple
from math   import pi

from comfy.k_diffusion.sampling import BrownianTreeNoiseSampler

from ..res4lyf import RESplain

# Set this to "True" if you have installed OpenSimplex. Recommended to install without dependencies due to conflicting packages: pip3 install opensimplex --no-deps 
OPENSIMPLEX_ENABLE = False

if OPENSIMPLEX_ENABLE:
    from opensimplex import OpenSimplex

class PrecisionTool:
    def __init__(self, cast_type='fp64'):
        self.cast_type = cast_type

    def cast_tensor(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if self.cast_type not in ['fp64', 'fp32', 'fp16']:
                return func(*args, **kwargs)

            target_device = None
            for arg in args:
                if torch.is_tensor(arg):
                    target_device = arg.device
                    break
            if target_device is None:
                for v in kwargs.values():
                    if torch.is_tensor(v):
                        target_device = v.device
                        break
            
        # recursively zs_recast tensors in nested dictionaries
            def cast_and_move_to_device(data):
                if torch.is_tensor(data):
                    if self.cast_type == 'fp64':
                        return data.to(torch.float64).to(target_device)
                    elif self.cast_type == 'fp32':
                        return data.to(torch.float32).to(target_device)
                    elif self.cast_type == 'fp16':
                        return data.to(torch.float16).to(target_device)
                elif isinstance(data, dict):
                    return {k: cast_and_move_to_device(v) for k, v in data.items()}
                return data

            new_args = [cast_and_move_to_device(arg) for arg in args]
            new_kwargs = {k: cast_and_move_to_device(v) for k, v in kwargs.items()}
            
            return func(*new_args, **new_kwargs)
        return wrapper

    def set_cast_type(self, new_value):
        if new_value in ['fp64', 'fp32', 'fp16']:
            self.cast_type = new_value
        else:
            self.cast_type = 'fp64'

precision_tool = PrecisionTool(cast_type='fp64')


def noise_generator_factory(cls, **fixed_params):
    def create_instance(**kwargs):
        params = {**fixed_params, **kwargs}
        return cls(**params)
    return create_instance

def like(x):
    return {'size': x.shape, 'dtype': x.dtype, 'layout': x.layout, 'device': x.device}

def scale_to_range(x, scaled_min = -1.73, scaled_max = 1.73): #1.73 is roughly the square root of 3
    return scaled_min + (x - x.min()) * (scaled_max - scaled_min) / (x.max() - x.min())

def normalize(x):
    return (x - x.mean())/ x.std()

class NoiseGenerator:
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None):
        self.seed = seed

        if x is not None:
            self.x      = x
            self.size   = x.shape
            self.dtype  = x.dtype
            self.layout = x.layout
            self.device = x.device
        else:   
            self.x      = torch.zeros(size, dtype, layout, device)

        # allow overriding parameters imported from latent 'x' if specified
        if size is not None:
            self.size   = size
        if dtype is not None:
            self.dtype  = dtype
        if layout is not None:
            self.layout = layout
        if device is not None:
            self.device = device

        self.sigma_max = sigma_max.to(device) if isinstance(sigma_max, torch.Tensor) else sigma_max
        self.sigma_min = sigma_min.to(device) if isinstance(sigma_min, torch.Tensor) else sigma_min

        self.last_seed = seed #- 1 #adapt for update being called during initialization, which increments last_seed
        
        if generator is None:
            self.generator = torch.Generator(device=self.device).manual_seed(seed)
        else:
            self.generator = generator

    def __call__(self):
        raise NotImplementedError("This method got clownsharked!")
    
    def update(self, **kwargs):
        
        #if not isinstance(self, BrownianNoiseGenerator):
        #    self.last_seed += 1
                    
        updated_values = []
        for attribute_name, value in kwargs.items():
            if value is not None:
                setattr(self, attribute_name, value)
            updated_values.append(getattr(self, attribute_name))
        return tuple(updated_values)



class BrownianNoiseGenerator(NoiseGenerator):
    def __call__(self, *, sigma=None, sigma_next=None, **kwargs):
        return BrownianTreeNoiseSampler(self.x, self.sigma_min, self.sigma_max, seed=self.seed, cpu = self.device.type=='cpu')(sigma, sigma_next)



class FractalNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                alpha=0.0, k=1.0, scale=0.1): 
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(alpha=alpha, k=k, scale=scale)

    def __call__(self, *, alpha=None, k=None, scale=None, **kwargs):
        self.update(alpha=alpha, k=k, scale=scale)
        self.last_seed += 1
        
        if len(self.size) == 5:
            b, c, t, h, w = self.size
        else:
            b, c, h, w = self.size
        
        noise = torch.normal(mean=0.0, std=1.0, size=self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)
        
        y_freq = torch.fft.fftfreq(h, 1/h, device=self.device)
        x_freq = torch.fft.fftfreq(w, 1/w, device=self.device)

        if len(self.size) == 5:
            t_freq = torch.fft.fftfreq(t, 1/t, device=self.device)
            freq = torch.sqrt(t_freq[:, None, None]**2 + y_freq[None, :, None]**2 + x_freq[None, None, :]**2).clamp(min=1e-10)
        else:
            freq = torch.sqrt(y_freq[:, None]**2 + x_freq[None, :]**2).clamp(min=1e-10)
        
        spectral_density = self.k / torch.pow(freq, self.alpha * self.scale)
        spectral_density[0, 0] = 0

        noise_fft = torch.fft.fftn(noise)
        modified_fft = noise_fft * spectral_density
        noise = torch.fft.ifftn(modified_fft).real

        return noise / torch.std(noise)
    
    

class SimplexNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                scale=0.01):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.noise = OpenSimplex(seed=seed)
        self.scale = scale
        
    def __call__(self, *, scale=None, **kwargs):
        self.update(scale=scale)
        self.last_seed += 1
        
        if len(self.size) == 5:
            b, c, t, h, w = self.size
        else:
            b, c, h, w = self.size

        noise_array = self.noise.noise3array(np.arange(w),np.arange(h),np.arange(c))
        self.noise = OpenSimplex(seed=self.noise.get_seed()+1)
        
        noise_tensor = torch.from_numpy(noise_array).to(self.device)
        noise_tensor = torch.unsqueeze(noise_tensor, dim=0)
        if len(self.size) == 5:
            noise_tensor = torch.unsqueeze(noise_tensor, dim=0)
        
        return noise_tensor / noise_tensor.std()
        #return normalize(scale_to_range(noise_tensor))



class HiresPyramidNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                discount=0.7, mode='nearest-exact'):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(discount=discount, mode=mode)

    def __call__(self, *, discount=None, mode=None, **kwargs):
        self.update(discount=discount, mode=mode)
        self.last_seed += 1

        if len(self.size) == 5:
            b, c, t, h, w = self.size
            orig_h, orig_w, orig_t = h, w, t
            u = nn.Upsample(size=(orig_h, orig_w, orig_t), mode=self.mode).to(self.device)
        else:
            b, c, h, w = self.size
            orig_h, orig_w = h, w
            orig_t = t = 1
            u = nn.Upsample(size=(orig_h, orig_w), mode=self.mode).to(self.device)

        noise = ((torch.rand(size=self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator) - 0.5) * 2 * 1.73)

        for i in range(4):
            r = torch.rand(1, device=self.device, generator=self.generator).item() * 2 + 2
            h, w = min(orig_h * 15, int(h * (r ** i))), min(orig_w * 15, int(w * (r ** i)))
            if len(self.size) == 5:
                t = min(orig_t * 15, int(t * (r ** i)))
                new_noise = torch.randn((b, c, t, h, w), dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)
            else:
                new_noise = torch.randn((b, c, h, w), dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)

            upsampled_noise = u(new_noise)
            noise += upsampled_noise * self.discount ** i
            
            if h >= orig_h * 15 or w >= orig_w * 15 or t >= orig_t * 15:
                break  # if resolution is too high
        
        return noise / noise.std()



class PyramidNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                discount=0.8, mode='nearest-exact'):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(discount=discount, mode=mode)

    def __call__(self, *, discount=None, mode=None, **kwargs):
        self.update(discount=discount, mode=mode)
        self.last_seed += 1

        x = torch.zeros(self.size, dtype=self.dtype, layout=self.layout, device=self.device)

        if len(self.size) == 5:
            b, c, t, h, w = self.size
            orig_h, orig_w, orig_t = h, w, t
        else:
            b, c, h, w = self.size
            orig_h, orig_w = h, w

        r = 1
        for i in range(5):
            r *= 2

            if len(self.size) == 5:
                scaledSize = (b, c, t * r, h * r, w * r)
                origSize = (orig_h, orig_w, orig_t)
            else:
                scaledSize = (b, c, h * r, w * r)
                origSize = (orig_h, orig_w)

            x += torch.nn.functional.interpolate(
                torch.normal(mean=0, std=0.5 ** i, size=scaledSize, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator),
                size=origSize, mode=self.mode
            ) * self.discount ** i
        return x / x.std()



class InterpolatedPyramidNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                discount=0.7, mode='nearest-exact'):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(discount=discount, mode=mode)

    def __call__(self, *, discount=None, mode=None, **kwargs):
        self.update(discount=discount, mode=mode)
        self.last_seed += 1

        if len(self.size) == 5:
            b, c, t, h, w = self.size
            orig_t, orig_h, orig_w = t, h, w
        else:
            b, c, h, w = self.size
            orig_h, orig_w = h, w
            t = orig_t = 1

        noise = ((torch.rand(size=self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator) - 0.5) * 2 * 1.73)
        multipliers = [1]

        for i in range(4):
            r = torch.rand(1, device=self.device, generator=self.generator).item() * 2 + 2
            h, w = min(orig_h * 15, int(h * (r ** i))), min(orig_w * 15, int(w * (r ** i)))
            
            if len(self.size) == 5:
                t = min(orig_t * 15, int(t * (r ** i)))
                new_noise = torch.randn((b, c, t, h, w), dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)
                upsampled_noise = nn.functional.interpolate(new_noise, size=(orig_t, orig_h, orig_w), mode=self.mode)
            else:
                new_noise = torch.randn((b, c, h, w), dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)
                upsampled_noise = nn.functional.interpolate(new_noise, size=(orig_h, orig_w), mode=self.mode)

            noise += upsampled_noise * self.discount ** i
            multipliers.append(        self.discount ** i)
            
            if h >= orig_h * 15 or w >= orig_w * 15 or (len(self.size) == 5 and t >= orig_t * 15):
                break  # if resolution is too high
        
        noise = noise / sum([m ** 2 for m in multipliers]) ** 0.5 
        return noise / noise.std()



class CascadeBPyramidNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                levels=10, mode='nearest', size_range=[1,16]):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(epsilon=x, levels=levels, mode=mode, size_range=size_range)

    def __call__(self, *, levels=10, mode='nearest', size_range=[1,16], **kwargs):
        self.update(levels=levels, mode=mode)
        if len(self.size) == 5:
            raise NotImplementedError("CascadeBPyramidNoiseGenerator is not implemented for 5D tensors (eg. video).") 
        self.last_seed += 1

        b, c, h, w = self.size

        epsilon = torch.randn(self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)
        multipliers = [1]
        for i in range(1, levels):
            m = 0.75 ** i

            h, w = int(epsilon.size(-2) // (2 ** i)), int(epsilon.size(-2) // (2 ** i))
            if size_range is None or (size_range[0] <= h <= size_range[1] or size_range[0] <= w <= size_range[1]):
                offset = torch.randn(epsilon.size(0), epsilon.size(1), h, w, device=self.device, generator=self.generator)
                epsilon = epsilon + torch.nn.functional.interpolate(offset, size=epsilon.shape[-2:], mode=self.mode) * m
                multipliers.append(m)

            if h <= 1 or w <= 1:
                break
        epsilon = epsilon / sum([m ** 2 for m in multipliers]) ** 0.5 #divides the epsilon tensor by the square root of the sum of the squared multipliers.

        return epsilon


class UniformNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                mean=0.0, scale=1.73):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(mean=mean, scale=scale)

    def __call__(self, *, mean=None, scale=None, **kwargs):
        self.update(mean=mean, scale=scale)
        self.last_seed += 1

        noise = torch.rand(self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)

        return self.scale * 2 * (noise - 0.5) + self.mean

class GaussianNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                mean=0.0, std=1.0):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(mean=mean, std=std)

    def __call__(self, *, mean=None, std=None, **kwargs):
        self.update(mean=mean, std=std)
        self.last_seed += 1

        noise = torch.randn(self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)

        return (noise - noise.mean()) / noise.std()

class GaussianBackwardsNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                mean=0.0, std=1.0):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(mean=mean, std=std)

    def __call__(self, *, mean=None, std=None, **kwargs):
        self.update(mean=mean, std=std)
        self.last_seed += 1
        RESplain("GaussianBackwards last seed:", self.generator.initial_seed())
        self.generator.manual_seed(self.generator.initial_seed() - 1)
        noise = torch.randn(self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator)

        return (noise - noise.mean()) / noise.std()

class LaplacianNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                loc=0, scale=1.0):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(loc=loc, scale=scale)

    def __call__(self, *, loc=None, scale=None, **kwargs):
        self.update(loc=loc, scale=scale)
        self.last_seed += 1

        # b, c, h, w = self.size
        # orig_h, orig_w = h, w

        noise = torch.randn(self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator) / 4.0

        rng_state = torch.random.get_rng_state()
        torch.manual_seed(self.generator.initial_seed())
        laplacian_noise = Laplace(loc=self.loc, scale=self.scale).rsample(self.size).to(self.device)
        self.generator.manual_seed(self.generator.initial_seed() + 1)
        torch.random.set_rng_state(rng_state)

        noise += laplacian_noise
        return noise / noise.std()

class StudentTNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                loc=0, scale=0.2, df=1):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(loc=loc, scale=scale, df=df)

    def __call__(self, *, loc=None, scale=None, df=None, **kwargs):
        self.update(loc=loc, scale=scale, df=df)
        self.last_seed += 1

        # b, c, h, w = self.size
        # orig_h, orig_w = h, w

        rng_state = torch.random.get_rng_state()
        torch.manual_seed(self.generator.initial_seed())

        noise = StudentT(loc=self.loc, scale=self.scale, df=self.df).rsample(self.size)
        if not isinstance(self, BrownianNoiseGenerator):
            self.last_seed += 1
                    
        s = torch.quantile(noise.flatten(start_dim=1).abs(), 0.75, dim=-1)
        
        if len(self.size) == 5:
            s = s.reshape(*s.shape, 1, 1, 1, 1)
        else:
            s = s.reshape(*s.shape, 1, 1, 1)

        noise = noise.clamp(-s, s)

        noise_latent = torch.copysign(torch.pow(torch.abs(noise), 0.5), noise).to(self.device)

        self.generator.manual_seed(self.generator.initial_seed() + 1)
        torch.random.set_rng_state(rng_state)
        return (noise_latent - noise_latent.mean()) / noise_latent.std()

class WaveletNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                wavelet='haar'):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(wavelet=wavelet)

    def __call__(self, *, wavelet=None, **kwargs):
        self.update(wavelet=wavelet)
        self.last_seed += 1

        # b, c, h, w = self.size
        # orig_h, orig_w = h, w

        # noise for spatial dimensions only
        coeffs = pywt.wavedecn(torch.randn(self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator).to('cpu'), wavelet=self.wavelet, mode='periodization')
        noise = pywt.waverecn(coeffs, wavelet=self.wavelet, mode='periodization')
        noise_tensor = torch.tensor(noise, dtype=self.dtype, device=self.device)

        noise_tensor = (noise_tensor - noise_tensor.mean()) / noise_tensor.std()
        return noise_tensor

class PerlinNoiseGenerator(NoiseGenerator):
    def __init__(self, x=None, size=None, dtype=None, layout=None, device=None, seed=42, generator=None, sigma_min=None, sigma_max=None, 
                detail=0.0):
        super().__init__(x, size, dtype, layout, device, seed, generator, sigma_min, sigma_max)
        self.update(detail=detail)

    @staticmethod
    def get_positions(block_shape: Tuple[int, int]) -> Tensor:
        bh, bw = block_shape
        positions = torch.stack(
            torch.meshgrid(
                [(torch.arange(b) + 0.5) / b for b in (bw, bh)],
                indexing="xy",
            ),
            -1,
        ).view(1, bh, bw, 1, 1, 2)
        return positions

    @staticmethod
    def unfold_grid(vectors: Tensor) -> Tensor:
        batch_size, _, gpy, gpx = vectors.shape
        return (
            unfold(vectors, (2, 2))
            .view(batch_size, 2, 4, -1)
            .permute(0, 2, 3, 1)
            .view(batch_size, 4, gpy - 1, gpx - 1, 2)
        )

    @staticmethod
    def smooth_step(t: Tensor) -> Tensor:
        return t * t * (3.0 - 2.0 * t)

    @staticmethod
    def perlin_noise_tensor(
        self,
        vectors: Tensor, positions: Tensor, step: Callable = None
    ) -> Tensor:
        if step is None:
            step = self.smooth_step

        batch_size = vectors.shape[0]
        # grid height, grid width
        gh, gw = vectors.shape[2:4]
        # block height, block width
        bh, bw = positions.shape[1:3]

        for i in range(2):
            if positions.shape[i + 3] not in (1, vectors.shape[i + 2]):
                raise Exception(
                    f"Blocks shapes do not match: vectors ({vectors.shape[1]}, {vectors.shape[2]}), positions {gh}, {gw})"
                )

        if positions.shape[0] not in (1, batch_size):
            raise Exception(
                f"Batch sizes do not match: vectors ({vectors.shape[0]}), positions ({positions.shape[0]})"
            )

        vectors = vectors.view(batch_size, 4, 1, gh * gw, 2)
        positions = positions.view(positions.shape[0], bh * bw, -1, 2)

        step_x = step(positions[..., 0])
        step_y = step(positions[..., 1])

        row0 = lerp(
            (vectors[:, 0] * positions).sum(dim=-1),
            (vectors[:, 1] * (positions - positions.new_tensor((1, 0)))).sum(dim=-1),
            step_x,
        )
        row1 = lerp(
            (vectors[:, 2] * (positions - positions.new_tensor((0, 1)))).sum(dim=-1),
            (vectors[:, 3] * (positions - positions.new_tensor((1, 1)))).sum(dim=-1),
            step_x,
        )
        noise = lerp(row0, row1, step_y)
        return (
            noise.view(
                batch_size,
                bh,
                bw,
                gh,
                gw,
            )
            .permute(0, 3, 1, 4, 2)
            .reshape(batch_size, gh * bh, gw * bw)
        )

    def perlin_noise(
        self,
        grid_shape: Tuple[int, int],
        out_shape: Tuple[int, int],
        batch_size: int = 1,
        generator: Generator = None,
        *args,
        **kwargs,
    ) -> Tensor:
        gh, gw = grid_shape         # grid height and width
        oh, ow = out_shape        # output height and width
        bh, bw = oh // gh, ow // gw        # block height and width

        if oh != bh * gh:
            raise Exception(f"Output height {oh} must be divisible by grid height {gh}")
        if ow != bw * gw != 0:
            raise Exception(f"Output width {ow} must be divisible by grid width {gw}")

        angle = torch.empty(
            [batch_size] + [s + 1 for s in grid_shape], device=self.device, *args, **kwargs
        ).uniform_(to=2.0 * pi, generator=self.generator)
        # random vectors on grid points
        vectors = self.unfold_grid(torch.stack((torch.cos(angle), torch.sin(angle)), dim=1))
        # positions inside grid cells [0, 1)
        positions = self.get_positions((bh, bw)).to(vectors)
        return self.perlin_noise_tensor(self, vectors, positions).squeeze(0)

    def __call__(self, *, detail=None, **kwargs):
        self.update(detail=detail) #currently unused
        self.last_seed += 1
        if len(self.size) == 5:
            b, c, t, h, w = self.size
            noise = torch.randn(self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator) / 2.0
            
            for tt in range(t):
                for i in range(2):
                    perlin_slice = self.perlin_noise((h, w), (h, w), batch_size=c, generator=self.generator).to(self.device)
                    perlin_expanded = perlin_slice.unsqueeze(0).unsqueeze(2)
                    time_slice = noise[:, :, tt:tt+1, :, :]
                    noise[:, :, tt:tt+1, :, :] += perlin_expanded
        else:
            b, c, h, w = self.size
            #orig_h, orig_w = h, w

            noise = torch.randn(self.size, dtype=self.dtype, layout=self.layout, device=self.device, generator=self.generator) / 2.0
            for i in range(2):
                noise += self.perlin_noise((h, w), (h, w), batch_size=c, generator=self.generator).to(self.device)
                
        return noise / noise.std()
    
from functools import partial

NOISE_GENERATOR_CLASSES = {
    "fractal"               :                         FractalNoiseGenerator,
    "gaussian"              :                         GaussianNoiseGenerator,
    "gaussian_backwards"    :                         GaussianBackwardsNoiseGenerator,
    "uniform"               :                         UniformNoiseGenerator,
    "pyramid-cascade_B"     :                         CascadeBPyramidNoiseGenerator,
    "pyramid-interpolated"  :                         InterpolatedPyramidNoiseGenerator,
    "pyramid-bilinear"      : noise_generator_factory(PyramidNoiseGenerator,      mode='bilinear'),
    "pyramid-bicubic"       : noise_generator_factory(PyramidNoiseGenerator,      mode='bicubic'),   
    "pyramid-nearest"       : noise_generator_factory(PyramidNoiseGenerator,      mode='nearest'),  
    "hires-pyramid-bilinear": noise_generator_factory(HiresPyramidNoiseGenerator, mode='bilinear'),
    "hires-pyramid-bicubic" : noise_generator_factory(HiresPyramidNoiseGenerator, mode='bicubic'),   
    "hires-pyramid-nearest" : noise_generator_factory(HiresPyramidNoiseGenerator, mode='nearest'),  
    "brownian"              :                         BrownianNoiseGenerator,
    "laplacian"             :                         LaplacianNoiseGenerator,
    "studentt"              :                         StudentTNoiseGenerator,
    "wavelet"               :                         WaveletNoiseGenerator,
    "perlin"                :                         PerlinNoiseGenerator,
}


NOISE_GENERATOR_CLASSES_SIMPLE = {
    "none"                  :                         GaussianNoiseGenerator,
    "brownian"              :                         BrownianNoiseGenerator,
    "gaussian"              :                         GaussianNoiseGenerator,
    "gaussian_backwards"    :                         GaussianBackwardsNoiseGenerator,
    "laplacian"             :                         LaplacianNoiseGenerator,
    "perlin"                :                         PerlinNoiseGenerator,
    "studentt"              :                         StudentTNoiseGenerator,
    "uniform"               :                         UniformNoiseGenerator,
    "wavelet"               :                         WaveletNoiseGenerator,
    "brown"                 : noise_generator_factory(FractalNoiseGenerator,      alpha=2.0),
    "pink"                  : noise_generator_factory(FractalNoiseGenerator,      alpha=1.0),
    "white"                 : noise_generator_factory(FractalNoiseGenerator,      alpha=0.0),
    "blue"                  : noise_generator_factory(FractalNoiseGenerator,      alpha=-1.0),
    "violet"                : noise_generator_factory(FractalNoiseGenerator,      alpha=-2.0),
    "ultraviolet_A"         : noise_generator_factory(FractalNoiseGenerator,      alpha=-3.0),
    "ultraviolet_B"         : noise_generator_factory(FractalNoiseGenerator,      alpha=-4.0),
    "ultraviolet_C"         : noise_generator_factory(FractalNoiseGenerator,      alpha=-5.0),

    "hires-pyramid-bicubic" : noise_generator_factory(HiresPyramidNoiseGenerator, mode='bicubic'),   
    "hires-pyramid-bilinear": noise_generator_factory(HiresPyramidNoiseGenerator, mode='bilinear'),
    "hires-pyramid-nearest" : noise_generator_factory(HiresPyramidNoiseGenerator, mode='nearest'),  
    "pyramid-bicubic"       : noise_generator_factory(PyramidNoiseGenerator,      mode='bicubic'),   
    "pyramid-bilinear"      : noise_generator_factory(PyramidNoiseGenerator,      mode='bilinear'),
    "pyramid-nearest"       : noise_generator_factory(PyramidNoiseGenerator,      mode='nearest'),  
    "pyramid-interpolated"  :                         InterpolatedPyramidNoiseGenerator,
    "pyramid-cascade_B"     :                         CascadeBPyramidNoiseGenerator,
}                        

if OPENSIMPLEX_ENABLE:
    NOISE_GENERATOR_CLASSES.update({
        "simplex": SimplexNoiseGenerator,
    })

NOISE_GENERATOR_NAMES = tuple(NOISE_GENERATOR_CLASSES.keys())
NOISE_GENERATOR_NAMES_SIMPLE = tuple(NOISE_GENERATOR_CLASSES_SIMPLE.keys())


@precision_tool.cast_tensor
def prepare_noise(latent_image, seed, noise_type, noise_inds=None, alpha=1.0, k=1.0, var_seed=None, var_strength=0.0): # adapted from comfy/sample.py: https://github.com/comfyanonymous/ComfyUI
    #optional arg skip can be used to skip and discard x number of noise generations for a given seed
    noise_func = NOISE_GENERATOR_CLASSES.get(noise_type)(x=latent_image, seed=seed, sigma_min=0.0291675, sigma_max=14.614642)                                          # WARNING: HARDCODED SDXL SIGMA RANGE!

    if noise_type == "fractal":
        noise_func.alpha = alpha
        noise_func.k = k

    # from here until return is very similar to comfy/sample.py 
    if noise_inds is None:
        base_noise = noise_func(sigma=14.614642, sigma_next=0.0291675)
        
        # SwarmUI-style variation seed implementation
        if var_seed is not None and var_strength > 0.0:
            batch_size = base_noise.shape[0]
            var_noises = []
            
            # Generate different variation noise for each batch item
            for i in range(batch_size):
                # Add batch index to variation seed for different patterns per batch item
                var_noise_func = NOISE_GENERATOR_CLASSES.get(noise_type)(x=latent_image[i:i+1], seed=var_seed + i, sigma_min=0.0291675, sigma_max=14.614642)
                
                if noise_type == "fractal":
                    var_noise_func.alpha = alpha
                    var_noise_func.k = k
                    
                var_noise = var_noise_func(sigma=14.614642, sigma_next=0.0291675)
                var_noises.append(var_noise)
            
            var_noise = torch.cat(var_noises, axis=0)
            
            # SLERP blend between base and variation noise
            from ..latents import slerp
            blended_noise = slerp(base_noise, var_noise, var_strength)
            return blended_noise
            
        return base_noise

    unique_inds, inverse = np.unique(noise_inds, return_inverse=True)
    noises = []
    for i in range(unique_inds[-1]+1):
        noise = noise_func(size = [1] + list(latent_image.size())[1:], dtype=latent_image.dtype, layout=latent_image.layout, device=latent_image.device)
        if i in unique_inds:
            noises.append(noise)
    noises = [noises[i] for i in inverse]
    noises = torch.cat(noises, axis=0)
    
    # Apply variation blending to batch noises if specified
    if var_seed is not None and var_strength > 0.0:
        var_noises = []
        for idx, i in enumerate(unique_inds):
            # Add batch index to variation seed for different patterns per batch item
            var_noise_func = NOISE_GENERATOR_CLASSES.get(noise_type)(x=latent_image, seed=var_seed + i, sigma_min=0.0291675, sigma_max=14.614642)
            
            if noise_type == "fractal":
                var_noise_func.alpha = alpha
                var_noise_func.k = k
                
            var_noise = var_noise_func(size = [1] + list(latent_image.size())[1:], dtype=latent_image.dtype, layout=latent_image.layout, device=latent_image.device)
            var_noises.append(var_noise)
            
        var_noises = [var_noises[i] for i in inverse]
        var_noises = torch.cat(var_noises, axis=0)
        
        # SLERP blend between base and variation noises
        from ..latents import slerp
        blended_noises = slerp(noises, var_noises, var_strength)
        return blended_noises
    
    return noises
