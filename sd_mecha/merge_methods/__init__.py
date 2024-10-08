import functools
import math
import operator
import numpy as np
import torch
from scipy.stats import binom
from torch import Tensor
from typing import Tuple, TypeVar, Dict, Optional
from sd_mecha.hypers import Hyper
from .svd import orthogonal_procrustes, fractional_matrix_power
from sd_mecha.merge_space import MergeSpace
from sd_mecha.extensions.merge_method import LiftFlag, convert_to_recipe


EPSILON = 1e-10
SameMergeSpace = TypeVar("SameMergeSpace", bound=LiftFlag[MergeSpace.BASE | MergeSpace.DELTA])


@convert_to_recipe
def weighted_sum(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    *,
    alpha: Hyper = 0.5,
    **kwargs,
) -> Tensor | SameMergeSpace:
    return (1 - alpha) * a + alpha * b


@convert_to_recipe
def n_average(
    *models: Tensor | SameMergeSpace,
    **kwargs,
) -> Tensor | SameMergeSpace:
    return torch.mean(torch.stack(models), dim=0)


@convert_to_recipe
def slerp(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    *,
    alpha: Hyper = 0.5,
    **kwargs,
) -> Tensor | SameMergeSpace:
    a_normalized = a / a.norm()
    b_normalized = b / b.norm()

    ab_dot = (a_normalized * b_normalized).sum().clamp(-1, 1)

    omega = torch.arccos(ab_dot)
    a_contrib = a_normalized * torch.sin((1-alpha)*omega)
    b_contrib = b_normalized * torch.sin(alpha*omega)
    res = (a_contrib + b_contrib) / torch.sin(omega)
    res *= weighted_sum.__wrapped__(a.norm(), b.norm(), alpha=alpha)
    if res.isnan().any():
        return weighted_sum.__wrapped__(a, b, alpha=alpha)
    return res


@convert_to_recipe
def add_difference(
    a: Tensor | SameMergeSpace,
    b: Tensor | LiftFlag[MergeSpace.DELTA],
    *,
    alpha: Hyper = 1.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    return a + alpha * b


@convert_to_recipe
def subtract(
    a: Tensor | LiftFlag[MergeSpace.BASE],
    b: Tensor | LiftFlag[MergeSpace.BASE],
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.DELTA]:
    return a - b


@convert_to_recipe
def perpendicular_component(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    **kwargs,
) -> Tensor | SameMergeSpace:
    norm_a = torch.linalg.norm(a)
    res = b - a * (a / norm_a * (b / norm_a)).sum()
    if res.isnan().any():
        return torch.zeros_like(a)
    return res


@convert_to_recipe
def geometric_sum(
    a: Tensor | LiftFlag[MergeSpace.DELTA],
    b: Tensor | LiftFlag[MergeSpace.DELTA],
    *,
    alpha: Hyper = 0.5,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.DELTA]:
    a = torch.complex(a, torch.zeros_like(a))
    b = torch.complex(b, torch.zeros_like(b))
    res = a ** (1 - alpha) * b ** alpha
    return res.real


@convert_to_recipe
def add_cosine_a(
    a: Tensor | LiftFlag[MergeSpace.BASE],
    b: Tensor | LiftFlag[MergeSpace.BASE],
    *,
    alpha: Hyper,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.BASE]:
    a_norm = torch.nn.functional.normalize(a, dim=0)
    b_norm = torch.nn.functional.normalize(b, dim=0)
    similarity = torch.nn.functional.cosine_similarity(a_norm, b_norm, dim=0)
    return add_cosine_generic(a, b, alpha, similarity)


@convert_to_recipe
def add_cosine_b(
    a: Tensor | LiftFlag[MergeSpace.BASE],
    b: Tensor | LiftFlag[MergeSpace.BASE],
    *,
    alpha: Hyper,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.BASE]:
    similarity = torch.nn.functional.cosine_similarity(a, b, dim=0)
    dot_product = torch.sum(a * b)
    magnitude_similarity = dot_product / (torch.norm(a) * torch.norm(b))
    combined_similarity = (similarity + magnitude_similarity) / 2.0
    return add_cosine_generic(a, b, alpha, combined_similarity)


def add_cosine_generic(a: Tensor, b: Tensor, alpha: float, similarity: Tensor) -> Tensor:
    k = 1 - torch.clamp(similarity - alpha, 0, 1)
    return weighted_sum.__wrapped__(a, b, alpha=k)


# Special mode "TIES-STOCK" has been implemented by setting `apply_stock` > 0.0
# Special mode "TIES-GMEDIAN" has been implemented by setting `apply_median` > 0.0
@convert_to_recipe
def ties_sum_extended(  # aka add_difference_ties
    *models: Tensor | LiftFlag[MergeSpace.DELTA],
    k: Hyper = 0.2,
    vote_sgn: Hyper = 0.0,
    apply_stock: Hyper = 0.0,
    cos_eps: Hyper = 1e-6,
    apply_median: Hyper = 0.0,
    eps: Hyper = 1e-6,
    maxiter: Hyper = 100,
    ftol: Hyper =1e-20,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.DELTA]:
    filtered_delta, param_counts = ties_sum_deltas(*models, k=k, vote_sgn=vote_sgn)

    if apply_median <= 0.0:
        # Model Stock
        t = 1.0 if apply_stock <= 0.0 else get_model_stock_t(torch.unbind(filtered_delta), cos_eps=cos_eps)

        filtered_delta = filtered_delta.sum(dim=0)

        # $$ \tau_m $$
        return torch.nan_to_num(filtered_delta * t / param_counts)
    else:
        # $$ \tau_m $$, but in geometric median instead of arithmetic mean. Considered to replace model stock.
        filtered_delta = geometric_median_list_of_array(torch.unbind(filtered_delta), eps=eps, maxiter=maxiter, ftol=ftol)

        return torch.nan_to_num(filtered_delta)


# latex notes in reference to original implementation: https://arxiv.org/abs/2306.01708
# - `delta`: $$ \hat{\tau}_t $$
# - `signs`: $$ \gamma_t $$
# - `final_sign`: $$ \gamma_m^p = sgn(\Sigma_{t=1}^n \hat{\tau}_t^p) $$
# - `delta_filters`: $$ \{ \gamma_t^p = \gamma_m^p \} $$
# - `param_counts`: $$ |A^p| $$
# - `filtered_delta`: $$ \Sigma_{t\in{A^p}} \hat{\tau}_t^p $$
# - `return`: $$ \lambda * \tau_m $$
# Special mode "TIES-SOUP" has been implemented by setting `vote_sgn` > 0.0
# - `final_sign`: $$ \gamma_m^p = sgn(\Sigma_{t=1}^n \gamma_t^p) $$
@convert_to_recipe
def ties_sum(  # aka add_difference_ties
    *models: Tensor | LiftFlag[MergeSpace.DELTA],
    k: Hyper = 0.2,
    vote_sgn: Hyper = 0.0,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.DELTA]:
    filtered_delta, param_counts = ties_sum_deltas(*models, k=k, vote_sgn=vote_sgn)

    # $$ \tau_m $$
    return torch.nan_to_num(filtered_delta.sum(dim=0) / param_counts)


def ties_sum_deltas(
    *models: Tensor,
    k: float = 0.2,
    vote_sgn: float = 0.0,
):
    # Step 1: Trim redundant parameters

    # $$ \hat{\tau}_t $$ O(N) in space
    deltas = [
        # $$ keep_topk_reset_rest_to_zero(\tau_t, k) $$
        filter_top_k(m, k)
        for m in models
    ]
    deltas = torch.stack(deltas, dim=0)

    # Step 2: Elect Final Signs.

    # $$ \gamma_t $$
    signs = torch.sign(deltas)

    # $$ \gamma_m^p = sgn(\Sigma_{t=1}^n \hat{\tau}_t^p) $$ for normal TIES
    # $$ \gamma_m^p = sgn(\Sigma_{t=1}^n \gamma_t^p) $$ if "TIES-SOUP" is activated
    final_sign = torch.sign(torch.sum(deltas if vote_sgn <= 0.0 else signs, dim=0))

    # Step 3: Disjoint merge.

    # $$ \{ \gamma_t^p = \gamma_m^p \} $$
    delta_filters = (signs == final_sign).float()

    # $$ |A^p| $$
    param_counts = torch.sum(delta_filters, dim=0)

    # $$ \Sigma_{t\in{A^P}} \hat{\tau}_t^p $$
    # (note that the sum is not performed here directly)
    filtered_delta = deltas * delta_filters

    return filtered_delta, param_counts


def filter_top_k(a: Tensor, k: float):
    k = max(int((1 - k) * torch.numel(a)), 1)
    k_value, _ = torch.kthvalue(torch.abs(a.flatten()).float(), k)
    top_k_filter = (torch.abs(a) >= k_value).float()
    return a * top_k_filter


@convert_to_recipe
def tensor_sum(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    *,
    width: Hyper = 0.5,
    offset: Hyper = 0.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    if a.shape == ():
        if width > 0.5:
            return b
        return a

    start_i, end_i, region_is_inverted = ratio_to_region(width, offset, a.size(0))
    if region_is_inverted:
        b[start_i:end_i] = a[start_i:end_i]
        return b
    else:
        a[start_i:end_i] = b[start_i:end_i]
        return a


@convert_to_recipe
def top_k_tensor_sum(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    *,
    width: Hyper = 0.5,
    offset: Hyper = 0.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    a_flat = torch.flatten(a)
    a_dist = torch.msort(a_flat)
    b_indices = torch.argsort(torch.flatten(b), stable=True)
    redist_indices = torch.argsort(b_indices)

    start_i, end_i, region_is_inverted = ratio_to_region(width, offset, torch.numel(a))
    start_top_k = kth_abs_value(a_dist, start_i)
    end_top_k = kth_abs_value(a_dist, end_i)

    indices_mask = (start_top_k <= torch.abs(a_dist)) & (torch.abs(a_dist) <= end_top_k)
    if region_is_inverted:
        indices_mask = ~indices_mask
    indices_mask = torch.gather(indices_mask.float(), 0, redist_indices)

    a_redist = torch.gather(a_dist, 0, redist_indices)
    a_redist = (1 - indices_mask) * a_flat + indices_mask * a_redist
    return a_redist.reshape_as(a)


def kth_abs_value(a: Tensor, k: int) -> Tensor:
    if k <= 0:
        return torch.tensor(-1, device=a.device)
    else:
        return torch.kthvalue(torch.abs(a.float()), k)[0]


def ratio_to_region(width: float, offset: float, n: int) -> Tuple[int, int, bool]:
    if width < 0:
        offset += width
        width = -width
    width = min(width, 1)

    if offset < 0:
        offset = 1 + offset - int(offset)
    offset = math.fmod(offset, 1.0)

    if width + offset <= 1:
        inverted = False
        start = offset * n
        end = (width + offset) * n
    else:
        inverted = True
        start = (width + offset - 1) * n
        end = offset * n

    return round(start), round(end), inverted


@convert_to_recipe
def train_difference(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    c: Tensor | SameMergeSpace,
    *,
    alpha: Hyper = 1.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    mask = 1.8 * torch.nan_to_num((b - a).abs() / ((b - a).abs() + (b - c).abs()), nan=0)
    return a + (b - c) * alpha * mask


@convert_to_recipe
def add_opposite(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    c: Tensor | SameMergeSpace,
    *,
    alpha: Hyper = 1.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    mask = 2 * torch.nan_to_num((a - b).abs() / ((a - b).abs() + (a + b - 2*c).abs()), nan=0)
    return a + (b - c) * alpha * mask


@convert_to_recipe
def clamped_add_opposite(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    c: Tensor | SameMergeSpace,
    *,
    alpha: Hyper = 1.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    threshold = torch.maximum(torch.abs(a - c), torch.abs(b - c))
    mask = torch.clamp(torch.nan_to_num((c - a) * (b - c) / threshold**2, nan=0), 0) * 2
    return a + (b - c) * alpha * mask


@convert_to_recipe
def select_max_delta(
    a: Tensor | LiftFlag[MergeSpace.DELTA],
    b: Tensor | LiftFlag[MergeSpace.DELTA],
    *,
    alpha: Hyper = 0.5,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.DELTA]:
    a_abs = (a / a.std()).abs()
    b_abs = (b / b.std()).abs()
    return torch.where((1 - alpha) * a_abs >= alpha * b_abs, a, b)


@convert_to_recipe
def multiply_quotient(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    c: Tensor | SameMergeSpace,
    *,
    alpha: Hyper = 1.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    ac_log = torch.log(a.abs()) - torch.log(c.abs())
    bc_log = torch.log(b.abs()) - torch.log(c.abs())

    b = torch.complex(b, torch.zeros_like(b))
    c = torch.complex(c, torch.zeros_like(c))

    threshold = torch.maximum(torch.abs(ac_log), torch.abs(bc_log))
    alpha *= torch.clamp(-torch.nan_to_num(ac_log * bc_log / threshold**2, nan=0), 0)

    res = a * (b / c)**alpha
    res = torch.where(torch.isnan(res), a, res)
    del a, b, c
    return res.real


@convert_to_recipe
def distribution_crossover(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    c: Tensor | SameMergeSpace,
    *,
    alpha: Hyper,
    tilt: Hyper,
    **kwargs,
) -> Tensor | SameMergeSpace:
    if alpha == 0:
        return a
    if alpha == 1:
        return b
    if tilt == 1 or a.shape == ():
        return weighted_sum.__wrapped__(a, b, alpha=alpha)

    c_indices = torch.argsort(torch.flatten(c))
    a_dist = torch.gather(torch.flatten(a), 0, c_indices)
    b_dist = torch.gather(torch.flatten(b), 0, c_indices)

    a_dft = torch.fft.rfft(a_dist)
    b_dft = torch.fft.rfft(b_dist)

    dft_filter = create_filter((a_dft.numel(),), alpha, tilt, device=a.device)

    x_dft = (1 - dft_filter) * a_dft + dft_filter * b_dft
    x_dist = torch.fft.irfft(x_dft, a_dist.shape[0])
    x_values = torch.gather(x_dist, 0, torch.argsort(c_indices))
    return x_values.reshape_as(a)


@convert_to_recipe
def crossover(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    *,
    alpha: Hyper = 0.5,
    tilt: Hyper = 0.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    if alpha == 0:
        return a
    if alpha == 1:
        return b
    if tilt == 1:
        return weighted_sum.__wrapped__(a, b, alpha=alpha)

    if len(a.shape) == 0 or torch.allclose(a.half(), b.half()):
        return weighted_sum.__wrapped__(a, b, alpha=tilt)

    shape = a.shape

    a_dft = torch.fft.rfftn(a, s=shape)
    b_dft = torch.fft.rfftn(b, s=shape)

    dft_filter = create_filter(a_dft.shape, alpha, tilt, device=a.device)

    x_dft = (1 - dft_filter)*a_dft + dft_filter*b_dft
    return torch.fft.irfftn(x_dft, s=shape)


def create_filter(shape: Tuple[int, ...] | torch.Size, alpha: float, tilt: float, device=None):
    """
    Create a crossover filter. The cut is first tilted around (0, 0), then slid along its normal until it touches the point (alpha, 1 - alpha).
    :param shape: shape of the filter
    :param alpha: the ratio between the low frequencies and high frequencies. must be in [0, 1]
      0 = all 0s, 1 = all 1s, 0s correspond to low frequencies and 1s correspond to high frequencies
    :param tilt: tilt of the filter. 0 = vertical filter, 0.5 = 45 degrees, 1 = degenerates to a weighted sum with alpha=alpha
    :param device: device of the filter
    :return:
    """
    if not 0 <= alpha <= 1:
        raise ValueError("alpha must be between 0 and 1")

    # normalize tilt to the range [0, 4]
    tilt -= math.floor(tilt // 4 * 4)
    if tilt > 2:
        alpha = 1 - alpha
        alpha_inverted = True
    else:
        alpha_inverted = False

    gradients = list(reversed([
        torch.linspace(0, 1, s, device=device)
        if i == 0 or s == 1 else
        # negative frequencies are in the second half of the dimension
        torch.cat([
            torch.linspace(0, (s - 1) // 2, s - s // 2, device=device),
            torch.linspace(s // 2, 1, s // 2, device=device)
        ]) / (s // 2)
        for i, s in enumerate(reversed(shape))
    ]))

    if len(shape) > 1:
        grids = torch.meshgrid(*(g**2 for g in gradients), indexing='ij')
        mesh = (torch.stack(grids).sum(dim=0) / len(shape)).sqrt()
    else:
        mesh = gradients[0]

    if tilt < EPSILON or abs(tilt - 4) < EPSILON:
        dft_filter = (mesh > 1 - alpha).float()
    elif abs(tilt - 2) < EPSILON:
        dft_filter = (mesh < 1 - alpha).float()
    else:
        tilt_cot = 1 / math.tan(math.pi * tilt / 2)
        if tilt <= 1 or 2 < tilt <= 3:
            dft_filter = mesh*tilt_cot + alpha*tilt_cot + alpha - tilt_cot
        else:  # 1 < tilt <= 2 or 3 < tilt
            dft_filter = mesh*tilt_cot - alpha*tilt_cot + alpha
        dft_filter = dft_filter.clip(0, 1)

    if alpha_inverted:
        dft_filter = 1 - dft_filter
    return dft_filter


@convert_to_recipe(volatile_hypers=["cache"])
def rotate(
    a: Tensor | SameMergeSpace,
    b: Tensor | SameMergeSpace,
    *,
    alignment: Hyper = 1.0,
    alpha: Hyper = 0.0,
    cache: Optional[Dict[str, Dict[str, Tensor]]] = None,
    **kwargs,
) -> Tensor | SameMergeSpace:
    if alignment == 0 and alpha == 0:
        return a

    if len(a.shape) < 2 or torch.allclose(a.half(), b.half()):
        return weighted_sum.__wrapped__(a, b, alpha=alpha)

    is_conv = len(a.shape) == 4 and a.shape[-1] != 1
    if is_conv:
        shape_2d = (-1, functools.reduce(operator.mul, a.shape[2:]))
    else:
        shape_2d = (a.shape[0], a.shape[1:].numel())

    a_neurons = a.reshape(*shape_2d)
    b_neurons = b.reshape(*shape_2d)
    a_centroid = a_neurons.mean(0)
    b_centroid = b_neurons.mean(0)
    a_neurons -= a_centroid
    b_neurons -= b_centroid

    alignment_is_float = alignment != round(alignment)

    if cache is not None:
        key = kwargs["key"]
        if key not in cache:
            cache[key] = {}
        cache = cache[key]

    if cache is not None and "rotation" in cache:
        rotation = transform = cache["rotation"].to(a.device, a.dtype)
    else:
        rotation = transform = orthogonal_procrustes(a_neurons, b_neurons, cancel_reflection=alignment_is_float)
        if cache is not None:
            cache["rotation"] = rotation.to("cpu", torch.float16)

    if alignment_is_float:
        transform = fractional_matrix_power(transform, alignment, cache)
    elif alignment == 0:
        transform = torch.eye(
            len(transform),
            dtype=transform.dtype,
            device=transform.device,
        )
    elif alignment != 1:
        transform = torch.linalg.matrix_power(transform, round(alignment))

    if alpha != 0:
        # interpolate the relationship between the neurons
        a_neurons = weighted_sum.__wrapped__(a_neurons, b_neurons @ rotation.T, alpha=alpha)

    a_neurons @= transform
    a_neurons += weighted_sum.__wrapped__(a_centroid, b_centroid, alpha=alignment)
    return a_neurons.reshape_as(a)


@convert_to_recipe
def clamp(
    a: Tensor | SameMergeSpace,
    *bounds: Tensor | SameMergeSpace,
    stiffness: Hyper = 0.0,
    **kwargs,
) -> Tensor | SameMergeSpace:
    maximums = functools.reduce(torch.maximum, bounds)
    minimums = functools.reduce(torch.minimum, bounds)
    bounds = torch.stack(bounds)
    average = bounds.mean(dim=0)

    if stiffness:
        smallest_positive = maximums
        largest_negative = minimums

        for i, bound in enumerate(bounds):
            smallest_positive = torch.where((smallest_positive >= bound) & (bound >= average), bound, smallest_positive)
            largest_negative = torch.where((largest_negative <= bound) & (bound <= average), bound, largest_negative)

        maximums = weighted_sum.__wrapped__(maximums, smallest_positive, alpha=stiffness)
        minimums = weighted_sum.__wrapped__(minimums, largest_negative, alpha=stiffness)

    return torch.minimum(torch.maximum(a, minimums), maximums)


@convert_to_recipe
def dropout(  # aka n-supermario
    delta0: Tensor | LiftFlag[MergeSpace.DELTA],
    *deltas: Tensor | LiftFlag[MergeSpace.DELTA],
    probability: Hyper = 0.9,
    rescale: Hyper = 1.0,
    overlap: Hyper = 1.0,
    overlap_emphasis: Hyper = 0.0,
    seed: Hyper = -1,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.DELTA]:
    if seed < 0:
        seed = None
    else:
        seed = int(seed)

    deltas = torch.stack((delta0,) + deltas)
    rng = np.random.default_rng(seed)

    if overlap % 2 == 1:
        masks = torch.stack([
            torch.from_numpy(rng.binomial(n=1, p=1 - probability, size=delta0.shape)).to(device=delta0.device, dtype=torch.bool)
            for _ in range(len(deltas))
        ])
    else:
        ks = np.arange(2 ** len(deltas))
        pmf = overlapping_sets_pmf(len(deltas), probability, overlap, overlap_emphasis)
        masks = torch.from_numpy(rng.choice(ks, size=delta0.shape, p=pmf)).to(delta0.device)
        masks = torch.stack([masks & 2 ** i != 0 for i in range(len(deltas))])

    final_delta = torch.zeros_like(delta0)
    for mask, delta in zip(masks, deltas):
        final_delta[mask] += delta[mask]

    if probability == 1.0:
        rescalar = 1.0
    else:
        rescalar = (1.0 - probability) ** rescale
        rescalar = rescalar if math.isfinite(rescalar) else 1
    return final_delta / masks.sum(0).clamp(1) / rescalar


# Part of TIES w/ DARE
# Hyperparameters defauled to values proposed to paper.
# Special mode "DROP" has been implemented by setting `no_rescale` > 0.0
# - `return`: $$ \hat{\delta}^t = \tilde{\delta}^t $$
@convert_to_recipe
def ties_sum_with_dropout(
    *deltas: Tensor | LiftFlag[MergeSpace.DELTA],
    probability: Hyper = 0.9,
    rescale: Hyper = 1.0,
    k: Hyper = 0.2,
    vote_sgn: Hyper = 0.0,
    apply_stock: Hyper = 0.0,
    cos_eps: Hyper = 1e-6,
    apply_median: Hyper = 0.0,
    eps: Hyper = 1e-6,
    maxiter: Hyper = 100,
    ftol: Hyper = 1e-20,
    seed: Hyper = -1,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.DELTA]:
    if not deltas or probability == 1:
        return 0

    generator = torch.Generator(deltas[0].device)
    if seed is not None and seed >= 0:
        generator.manual_seed(round(seed))

    # Under "Dropout", delta will be 0 by definition. Multiply it (Hadamard product) will return 0 also.
    # $$ \tilde{\delta}^t = (1 - m^t) \odot \delta^t $$
    deltas = [delta * torch.bernoulli(torch.full(delta.shape, 1 - probability, device=delta.device, dtype=delta.dtype), generator=generator) for delta in deltas]

    # $$ \tilde{\delta}^t = \tau_m = \hat{\tau}_t $$ O(N) in space
    deltas = ties_sum_extended.__wrapped__(*deltas, k=k, vote_sgn=vote_sgn, apply_stock=apply_stock, cos_eps=cos_eps, apply_median=apply_median, eps=eps, maxiter=maxiter, ftol=ftol)

    if probability == 1.0:
        rescalar = 1.0
    else:
        rescalar = (1.0 - probability) ** rescale
        rescalar = rescalar if math.isfinite(rescalar) else 1
    return deltas / rescalar


def overlapping_sets_pmf(n, p, overlap, overlap_emphasis):
    if np.isclose(overlap, round(overlap)):
        if round(overlap) % 2 == 0:
            pmf = np.array([1/n*float(bin(i).count("1") == 1) for i in range(1, 2**n)])
        else:
            pmf = np.array([0 for _ in range(1, 2**n - 1)] + [1])
    else:
        if math.floor(overlap) % 2 == 1:
            overlap = -overlap

        tan_overlap = np.tan(np.pi * (overlap - 0.5))
        pmf = np.zeros(2 ** n - 1)
        for i in range(1, 2 ** n):
            num_sets = bin(i).count("1")
            pmf[i-1] = tan_overlap*(num_sets - n/2)
        pmf = np.exp(pmf) / np.sum(np.exp(pmf))

    binomial_pmf = binom.pmf(np.arange(1, n + 1), n, p)
    expanded_binomial_pmf = np.zeros(2 ** n - 1)
    for i in range(1, 2 ** n):
        num_sets = bin(i).count("1")
        expanded_binomial_pmf[i-1] = binomial_pmf[num_sets-1] / binomial_coefficient_np(n, num_sets)
    expanded_binomial_pmf /= expanded_binomial_pmf.sum()

    pmf = weighted_sum.__wrapped__(
        pmf,
        weighted_sum.__wrapped__(pmf, expanded_binomial_pmf, alpha=1-abs(2*overlap-1)),
        alpha=overlap_emphasis,
    )
    return np.concatenate([[p], pmf * (1 - p)])


def binomial_coefficient_np(n, k):
    if k > n - k:
        k = n - k
    result = np.int64(1)
    for i in range(1, k+1):
        result = result * (n - i + 1) // i
    return result


# Following mergekit's implementation of Model Stock (which official implementation doesn't exist)
# https://github.com/arcee-ai/mergekit/blob/main/mergekit/merge_methods/model_stock.py
# I will break the functions to be retrivible for other algos like TIES.
@convert_to_recipe
def model_stock_for_tensor(
    *deltas: Tensor | LiftFlag[MergeSpace.DELTA],
    cos_eps: Hyper = 1e-6,
    **kwargs,
) -> Tensor | LiftFlag[MergeSpace.DELTA]:

    # This is obvious.
    w_avg = n_average.__wrapped__(*deltas)

    # t can get inf so handle with care
    t = get_model_stock_t(deltas, cos_eps)

    # return w_h. Notice that w_0 is 0 here.
    return torch.nan_to_num(t * w_avg)


# The guess from mergekit: Average of cos(theta). Expected value is 0, somehow match with paper.
# However this may be very unstable, and the range is still -1 to 1.
def get_model_stock_t(deltas, cos_eps):
    n = len(deltas)

    # Generator function. Default eps from torch API doc.
    cos = torch.nn.CosineSimilarity(dim=-1, eps=cos_eps)

    # One-liner is all you need. I may make it in running average if it really memory hungry.
    cos_thetas = [cos(deltas[i], deltas[i + 1]) for i, _ in enumerate(deltas) if (i + 1) < n]

    # Still a vector.
    cos_theta = torch.stack(cos_thetas).mean(dim=0)

    # Convert to column vector for multiplication.
    t = (n * cos_theta / (1 + (n - 1) * cos_theta)).unsqueeze(-1)

    return t


# This becomes a wrapper since I want TIES use GM also.
@convert_to_recipe
def geometric_median(
    *models: Tensor | SameMergeSpace,
    eps: Hyper = 1e-6,
    maxiter: Hyper = 100,
    ftol: Hyper = 1e-20,
    **kwargs,
) -> Tensor | SameMergeSpace:
    return geometric_median_list_of_array(models, eps, maxiter, ftol)


# Original sourcecode: https://github.com/krishnap25/geom_median/blob/main/src/geom_median/torch/weiszfeld_list_of_array.py
# Changed to "List comprehension" and rely on torch API only. It is now fully parallel.
def geometric_median_list_of_array(models, eps, maxiter, ftol):
    # I think it is impossible to pass this from user space so I hardcode this instead.
    # Meanwhile I rename "points" as "models"
    # no_grad part is rare case: Merge algorithm under GPU is never heard.
    weights = torch.ones(len(models), device=models[0].device)

    # initialize median estimate at mean
    median = weighted_average(models, weights)
    new_weights = weights
    objective_value = geometric_median_objective(median, models, weights)

    # Weiszfeld iterations
    for _ in range(max(0, round(maxiter))):
        prev_obj_value = objective_value
        denom = torch.stack([l2distance(p, median) for p in models])
        new_weights = weights / torch.clamp(denom, min=eps)
        median = weighted_average(models, new_weights)

        objective_value = geometric_median_objective(median, models, weights)
        if abs(prev_obj_value - objective_value) <= ftol * objective_value:
            break

    return weighted_average(models, new_weights)


def weighted_average(points, weights):
    # weighted_average_component is not even required.
    return torch.sum(torch.stack([p * weights[i] for i, p in enumerate(points)]), dim=0) / weights.sum()


def geometric_median_objective(median, points, weights):
    return torch.mean(torch.stack([l2distance(point, median) for point in points]) * weights)


def l2distance(p1, p2):
    return torch.dist(p1, p2, p=2)
