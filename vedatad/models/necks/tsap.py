import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange, repeat

from vedacore.misc import registry


class Mlp(nn.Module):
    def __init__(
        self,
        in_features,
        hidden_features=None,
        out_features=None,
        act_layer=nn.GELU,
        drop_rate=0.0,
    ):
        super().__init__()
        self.drop_rate = drop_rate
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        if self.drop_rate > 0.0:
            self.drop = nn.Dropout(drop_rate)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        if self.drop_rate > 0.0:
            x = self.drop(x)
        x = self.fc2(x)
        if self.drop_rate > 0.0:
            x = self.drop(x)
        return x


def drop_path(x, drop_prob: float = 0.0, training: bool = False):
    """
    Stochastic Depth per sample.
    """
    if drop_prob == 0.0 or not training:
        return x
    keep_prob = 1 - drop_prob
    shape = (x.shape[0],) + (1,) * (
        x.ndim - 1
    )  # work with diff dim tensors, not just 2D ConvNets
    mask = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
    mask.floor_()  # binarize
    output = x.div(keep_prob) * mask
    return output


class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks)."""

    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)


class SpatialPooler(nn.Module):
    def __init__(self, name, **kwargs):
        super().__init__()
        self.name = name

        if name == 'avg':
            self.pooler = F.adaptive_avg_pool3d
        elif name == 'max':
            self.pooler = F.adaptive_max_pool3d
        elif name == 'twpool':
            self.pooler = TemporalWiseAttentionPooling(**kwargs)
        else:
            assert NotImplementedError

    def forward(self, x):
        if self.name in ['avg', 'max']:
            return self.pooler(x, (None, 1, 1)).flatten(2)
        else:
            return self.pooler(x)

def get_rel_pos(rel_pos, d):
    if isinstance(d, int):
        ori_d = rel_pos.shape[0]
        if ori_d == d:
            return rel_pos
        else:
            # Interpolate rel pos.
            new_pos_embed = F.interpolate(
                rel_pos.reshape(1, ori_d, -1).permute(0, 2, 1),
                size=d,
                mode="linear",
            )

            return new_pos_embed.reshape(-1, d).permute(1, 0)

def cal_rel_pos_spatial(
    attn, q, k, has_cls_embed, q_shape, k_shape, rel_pos_h, rel_pos_w
):
    """
    Decomposed Spatial Relative Positional Embeddings.
    """
    sp_idx = 1 if has_cls_embed else 0
    q_t, q_h, q_w = q_shape
    k_t, k_h, k_w = k_shape
    dh = int(2 * max(q_h, k_h) - 1)
    dw = int(2 * max(q_w, k_w) - 1)

    # Scale up rel pos if shapes for q and k are different.
    q_h_ratio = max(k_h / q_h, 1.0)
    k_h_ratio = max(q_h / k_h, 1.0)
    dist_h = (
        torch.arange(q_h)[:, None] * q_h_ratio
        - torch.arange(k_h)[None, :] * k_h_ratio
    )
    dist_h += (k_h - 1) * k_h_ratio
    q_w_ratio = max(k_w / q_w, 1.0)
    k_w_ratio = max(q_w / k_w, 1.0)
    dist_w = (
        torch.arange(q_w)[:, None] * q_w_ratio
        - torch.arange(k_w)[None, :] * k_w_ratio
    )
    dist_w += (k_w - 1) * k_w_ratio

    # Intepolate rel pos if needed.
    rel_pos_h = get_rel_pos(rel_pos_h, dh)
    rel_pos_w = get_rel_pos(rel_pos_w, dw)
    Rh = rel_pos_h[dist_h.long()]
    Rw = rel_pos_w[dist_w.long()]

    B, n_head, q_N, dim = q.shape

    r_q = q[:, :, sp_idx:].reshape(B, n_head, q_t, q_h, q_w, dim)
    rel_h_q = torch.einsum(
        "bythwc,hkc->bythwk", r_q, Rh
    )  # [B, H, q_t, qh, qw, k_h]
    rel_w_q = torch.einsum(
        "bythwc,wkc->bythwk", r_q, Rw
    )  # [B, H, q_t, qh, qw, k_w]

    attn[:, :, sp_idx:, sp_idx:] = (
        attn[:, :, sp_idx:, sp_idx:].view(B, -1, q_t, q_h, q_w, k_t, k_h, k_w)
        + rel_h_q[:, :, :, :, :, None, :, None]
        + rel_w_q[:, :, :, :, :, None, None, :]
    ).view(B, -1, q_t * q_h * q_w, k_t * k_h * k_w)

    return attn

class SpatialAttention(nn.Module):
    def __init__(
        self,
        base_dim,
        num_heads,
        qkv_bias=True,
        drop_rate=0.0,
    ):
        super().__init__()

        self.base_dim = base_dim
        self.num_heads = num_heads
        self.drop_rate = drop_rate
        head_dim = base_dim // num_heads
        self.scale = head_dim ** -0.5

        self.qkv = nn.Linear(base_dim, base_dim * 3, bias=qkv_bias)
        self.proj = nn.Linear(base_dim, base_dim)

        if drop_rate > 0.0:
            self.proj_drop = nn.Dropout(drop_rate)

        rel_sp_dim = 2 * 7 - 1
        self.rel_pos_h = nn.Parameter(torch.zeros(rel_sp_dim, head_dim))
        self.rel_pos_w = nn.Parameter(torch.zeros(rel_sp_dim, head_dim))

    def forward(self, x, h, w):
        b, t, n, c = x.size()
        qkv = self.qkv(x)
        qkv = rearrange(qkv, "b t n (qkv h c) -> qkv (b h t) c n", qkv=3, h=self.num_heads)

        q, k, v = qkv[0], qkv[1], qkv[2]
        q = rearrange(q, "(b h t) c n -> (b t) h n c", b=x.size(0), h=self.num_heads)
        k = rearrange(k, "(b h t) c n -> (b t) h n c", b=x.size(0), h=self.num_heads)
        v = rearrange(v, "(b h t) c n -> (b t) h n c", b=x.size(0), h=self.num_heads)

        attn = (q * self.scale) @ k.transpose(-2, -1)
        attn = cal_rel_pos_spatial(
            attn, q, k, True,
            (1, h, w),
            (1, h, w),
            self.rel_pos_h,
            self.rel_pos_w,
        )
        attn = attn.softmax(dim=-1)
        x = attn @ v
        x = rearrange(x, "(b t) h n c -> b t n (h c)", b=b, t=t)

        x = self.proj(x)
        if self.drop_rate > 0.0:
            x = self.proj_drop(x)

        return x

class AttentionLayer(nn.Module):
    def __init__(
        self,
        base_dim,
        num_heads,
        mlp_ratio=4.0,
        qkv_bias=True,
        drop_rate=0.0,
        drop_path=0.0,
        act_layer=nn.GELU,
        norm_layer=nn.LayerNorm
    ):
        super().__init__()

        self.base_dim = base_dim
        self.num_heads = num_heads
        self.mlp_ratio = mlp_ratio

        self.norm1 = norm_layer(base_dim)
        self.attn = SpatialAttention(base_dim, num_heads, qkv_bias=qkv_bias, drop_rate=drop_rate)
        self.norm2 = norm_layer(base_dim)
        self.mlp = Mlp(base_dim, int(base_dim * mlp_ratio), act_layer=act_layer, drop_rate=drop_rate)
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()

    def forward(self, x, h, w):
        x = self.drop_path(self.attn(self.norm1(x), h, w)) + x
        x = self.drop_path(self.mlp(self.norm2(x))) + x
        return x

@registry.register_module("neck")
class TemporalWiseAttentionPooling(nn.Module):
    def __init__(
        self,
        input_dim,
        base_dim,
        output_dim=None,
        num_heads=8,
        mlp_ratio=4.0,
        qkv_bias=False,
        drop_rate=0.0,
        drop_path=0.0,
        act_layer=nn.GELU,
        norm_layer=nn.LayerNorm,
        num_layers=4,
        skip='max'
    ):
        super().__init__()
        self.input_dim = input_dim
        self.base_dim = base_dim
        self.output_dim = base_dim if output_dim is None else output_dim
        self.num_heads = num_heads
        self.qkv_bias = qkv_bias
        self.drop_rate = drop_rate
        self.norm_layer = norm_layer
        self.num_layers = num_layers

        self.proj = nn.Linear(input_dim, base_dim)
        self.layers = nn.ModuleList([
            AttentionLayer(
                base_dim,
                num_heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                drop_rate=drop_rate,
                drop_path=drop_path,
                act_layer=act_layer,
                norm_layer=norm_layer
            )
            for i in range(num_layers)
        ])
        self.norm = norm_layer(base_dim)

        self.cls_token = nn.Embedding(1, base_dim)
        self.skip = skip
        if skip is not None:
            self.pool_skip = SpatialPooler(skip)
            if input_dim != self.output_dim:
                self.pool_proj = nn.Linear(input_dim, base_dim)
            else:
                self.pool_proj = nn.Identity()

        if base_dim != self.output_dim:
            self.output_proj = nn.Linear(base_dim, self.output_dim)
        else:
            self.output_proj = nn.Identity()

    def init_weights(self):
        pass
        # """Initiate the parameters."""
        # for m in self.modules():
        #     if isinstance(m, _ConvNd):
        #         kaiming_init(m)
        #     elif isinstance(m, _BatchNorm):
        #         constant_init(m, 1)

    def forward(self, x):
        b, c, t, h, w = x.size()

        if self.skip is not None:
            pool_skip = self.pool_skip(x)
            pool_skip = rearrange(pool_skip, 'b c ... -> b ... c')
            pool_skip = self.pool_proj(pool_skip)
        x = rearrange(x, 'b c t h w -> b t (h w) c')
        x = self.proj(x)

        cls_token = repeat(self.cls_token.weight, 'b c -> (b b1) t () c', b1=b, t=t)
        x = torch.cat([cls_token, x], dim=2)

        for layer in self.layers:
            x = layer(x, h, w)

        x = self.norm(x)
        x = x[:, :, 0]
        x = self.output_proj(x)
        if self.skip is not None:
            x + pool_skip
        x = rearrange(x, 'b t c -> b c t')

        return x



