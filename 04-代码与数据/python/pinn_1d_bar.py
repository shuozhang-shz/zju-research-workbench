# -*- coding: utf-8 -*-
"""
pinn_1d_bar.py
==============
物理信息神经网络（PINN）入门模板：求解一维弹性杆的轴向变形。

问题（正向）：
    EA u''(x) + q = 0 ,  x ∈ [0, L]
    u(0) = 0 ,  u(L) = 0          （两端固支，均布载荷 q）
    解析解： u(x) = q x (L - x) / (2EA)

问题（反向，--inverse）：
    位移 u(x) 部分已知、弹性模量 E 未知 → 把 E 设为可训练参数，
    用「数据损失 + 物理残差」同时辨识 E。这是"参数反演"类研究的最小雏形。

运行：
    python pinn_1d_bar.py                # 正向问题
    python pinn_1d_bar.py --inverse      # 反问题：辨识 E
    python pinn_1d_bar.py --steps 20000  # 训练步数

研究提示（如何往软物质课题上改）：
  1) 把 EA u'' + q 换成你的控制方程，例如超弹性杆：dP(F)/dx + b = 0，
     其中 P(F) = dW/dF（见 06-知识库/本构模型速查.md）；
  2) 自动微分天然给出 dP/dx，无需写弱形式；
  3) 增加数据损失项即可做"本构参数反演 / 场重构"；
  4) 二维问题：配点改为网格采样，残差为平衡方程的两个分量。
"""
import argparse

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import torch
import torch.nn as nn

torch.manual_seed(0)
np.random.seed(0)

# ---------------- 问题参数 ----------------
L, A, Q = 1.0, 1.0, 100.0   # 杆长 / 截面积 / 均布载荷
E_TRUE = 10.0               # 真实杨氏模量（反向问题中视为未知）

device = "cuda" if torch.cuda.is_available() else "cpu"


class PINN(nn.Module):
    def __init__(self, layers=(1, 32, 32, 32, 1)):
        super().__init__()
        seq, n = [], len(layers) - 1
        for i in range(n):
            seq.append(nn.Linear(layers[i], layers[i + 1]))
            if i < n - 1:
                seq.append(nn.Tanh())
        self.net = nn.Sequential(*seq)

    def forward(self, x):
        return self.net(x)


def residual(model, x, E):
    """物理残差 r(x) = E*A*u'' + q （自动微分求 u''）"""
    x = x.requires_grad_(True)
    u = model(x)
    u_x = torch.autograd.grad(u, x, grad_outputs=torch.ones_like(u), create_graph=True)[0]
    u_xx = torch.autograd.grad(u_x, x, grad_outputs=torch.ones_like(u_x), create_graph=True)[0]
    return E * A * u_xx + Q


def exact_u(x, E=E_TRUE):
    return Q * x * (L - x) / (2 * E * A)


def train(inverse=False, steps=8000, n_col=64, lr=1e-3):
    model = PINN().to(device)
    # 反问题：E 作为可训练标量
    E = nn.Parameter(torch.tensor([3.0], device=device)) if inverse else torch.tensor(E_TRUE, device=device)
    params = list(model.parameters()) + ([E] if inverse else [])
    opt = torch.optim.Adam(params, lr=lr)
    mse = nn.MSELoss()

    x_col = torch.rand(n_col, 1, device=device) * L          # 配点
    x_bc = torch.tensor([[0.0], [L]], device=device)          # 边界点
    # 反问题的"实验"位移数据：中段 9 个点，加 1% 噪声
    x_obs = torch.linspace(0.1, 0.9, 9, device=device).reshape(-1, 1)
    y_obs = exact_u(x_obs) * (1 + 0.01 * torch.randn_like(x_obs))

    losses = []
    for step in range(steps):
        opt.zero_grad()
        r = residual(model, x_col, E)
        loss_f = mse(r, torch.zeros_like(r))
        loss_b = mse(model(x_bc), torch.zeros_like(model(x_bc)))
        loss = loss_f + loss_b
        if inverse:
            loss = loss + mse(model(x_obs), y_obs) * 100.0
        loss.backward()
        opt.step()
        losses.append(loss.item())
        if step % (steps // 8) == 0:
            msg = f"step {step:6d}  total {loss.item():.3e}  (physics {loss_f.item():.3e}, bc {loss_b.item():.3e})"
            if inverse:
                msg += f"  E = {E.item():.4f} (true {E_TRUE})"
            print(msg)
    return model, E, losses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inverse", action="store_true", help="反问题：从位移数据辨识 E")
    ap.add_argument("--steps", type=int, default=8000)
    args = ap.parse_args()

    model, E, losses = train(inverse=args.inverse, steps=args.steps)

    if args.inverse:
        print(f"\n[result] 辨识得到 E = {E.item():.4f}，真值 {E_TRUE}，相对误差 "
              f"{abs(E.item()-E_TRUE)/E_TRUE*100:.2f}%")

    # ---------- 可视化 ----------
    x = torch.linspace(0, L, 200, device=device).reshape(-1, 1)
    with torch.no_grad():
        u_pred = model(x).cpu().numpy()
    x_np = x.cpu().numpy()
    u_exact = exact_u(x_np)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2))
    ax1.plot(x_np, u_exact, "k--", lw=2, label="exact")
    ax1.plot(x_np, u_pred, color="tab:red", lw=1.6, label="PINN")
    ax1.set_xlabel("x"); ax1.set_ylabel("displacement u(x)")
    ax1.set_title("PINN vs exact solution" + (" (inverse)" if args.inverse else " (forward)"))
    ax1.legend(); ax1.grid(alpha=.3)

    ax2.semilogy(losses)
    ax2.set_xlabel("training step"); ax2.set_ylabel("total loss")
    ax2.set_title("Training convergence"); ax2.grid(alpha=.3, which="both")

    out = "pinn_1d_bar_inverse.png" if args.inverse else "pinn_1d_bar.png"
    fig.tight_layout(); fig.savefig(out, dpi=160)
    print(f"[plot] 已保存 -> {out}")


if __name__ == "__main__":
    main()
