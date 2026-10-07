# -*- coding: utf-8 -*-
"""
neural_constitutive.py
======================
数据驱动本构（neural constitutive model）最小示例：
用一个 MLP 从单轴拉伸数据中学习 应力 P = NN(λ)，并与真实 Yeoh 模型对比。

核心教育目的：
  1) 机器学习可以拟合本构关系，但【外推（训练范围之外）不可信】；
  2) 看图体会：训练区间内拟合很好，λ > 3 之后偏离真实 Yeoh 响应；
  3) 改进方向（详见 06-知识库/机器学习/MLx软物质入门路线.md）：
     - 输入不变量 (Ī1, Ī2) 而不是 λ，保证客观性；
     - 输出物理约束：P(λ=1)=0、单调性、能量守恒（超弹性）；
     - 用网络参数化应变能 ψ（保正、凸性），再自动微分得应力。

运行：
    python neural_constitutive.py
"""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import torch
import torch.nn as nn

torch.manual_seed(0)
np.random.seed(0)

# ---------------- 真实模型（ Yeoh，单位 kPa）生成"实验"数据 ----------------
def yeoh_P(lam, C10=15.0, C20=2.0, C30=0.5):
    I1 = lam ** 2 + 2.0 / lam
    return 2.0 * (lam - lam ** -2) * (C10 + 2 * C20 * (I1 - 3) + 3 * C30 * (I1 - 3) ** 2)

LAMBDA_MAX_TRAIN = 3.0     # 实验能达到的最大拉伸比
lam_train = np.linspace(1.0, LAMBDA_MAX_TRAIN, 30)
P_train = yeoh_P(lam_train) * (1 + np.random.normal(0, 0.02, lam_train.size))
P_train[0] = 0.0

# ---------------- MLP：λ -> P ----------------
class ConstitutiveNN(nn.Module):
    def __init__(self, hidden=32):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(1, hidden), nn.Tanh(),
            nn.Linear(hidden, hidden), nn.Tanh(),
            nn.Linear(hidden, 1),
        )

    def forward(self, x):
        return self.net(x)


def train(lam, P, steps=6000, lr=1e-3):
    model = ConstitutiveNN()
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.StepLR(opt, step_size=2000, gamma=0.5)
    x = torch.tensor(lam, dtype=torch.float32).reshape(-1, 1)
    y = torch.tensor(P, dtype=torch.float32).reshape(-1, 1)
    for step in range(steps):
        opt.zero_grad()
        loss = nn.functional.mse_loss(model(x), y)
        loss.backward()
        opt.step(); sched.step()
        if step % (steps // 5) == 0:
            print(f"step {step:5d}  MSE = {loss.item():.4e}")
    return model


def main():
    model = train(lam_train, P_train)
    torch.save(model.state_dict(), "neural_constitutive.pt")
    print("[save] 模型权重 -> neural_constitutive.pt")

    # 外推区间：训练只见过 λ ≤ 3，看看 λ ∈ (3, 4] 会发生什么
    lam_test = np.linspace(1.0, 4.0, 300)
    with torch.no_grad():
        P_nn = model(torch.tensor(lam_test, dtype=torch.float32).reshape(-1, 1)).numpy().ravel()

    fig, ax = plt.subplots(figsize=(7.5, 5))
    ax.plot(lam_train, P_train, "ko", ms=5, mfc="none", label="data (noisy)")
    ax.plot(lam_test, yeoh_P(lam_test), "k--", lw=2, label="true Yeoh")
    ax.plot(lam_test, P_nn, color="tab:red", lw=2, label="neural network")
    ax.axvspan(LAMBDA_MAX_TRAIN, 4.0, color="tab:red", alpha=.07)
    ax.text((LAMBDA_MAX_TRAIN + 4) / 2, ax.get_ylim()[0] * .1 + 2, "extrapolation\n(unreliable!)",
            ha="center", fontsize=9, color="tab:red")
    ax.axvline(LAMBDA_MAX_TRAIN, color="gray", ls=":", lw=1)
    ax.set_xlabel("stretch $\\lambda$"); ax.set_ylabel("nominal stress $P$ (kPa)")
    ax.set_title("Neural constitutive model: fit is good, extrapolation is NOT")
    ax.legend(); ax.grid(alpha=.3)
    fig.tight_layout(); fig.savefig("neural_constitutive.png", dpi=160)
    print("[plot] 已保存 -> neural_constitutive.png")
    print("\n思考：如何让网络『懂物理』？——见脚本头部注释与知识库入门路线。")


if __name__ == "__main__":
    main()
