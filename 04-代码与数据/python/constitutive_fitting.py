# -*- coding: utf-8 -*-
"""
constitutive_fitting.py
=======================
将常见不可压缩超弹性本构模型拟合到【单轴拉伸】试验数据。

适用对象：软物质力学（橡胶 / 水凝胶 / 弹性体）单轴拉伸的名义应力-拉伸比数据。

数据格式（CSV，两列，含表头）：
    stretch, stress
    1.0,     0.0
    1.2,     3.1
    ...      ...
    # stretch = lambda（拉伸比），stress = P（名义/工程应力，单位自定，kPa 或 MPa）

用法：
    python constitutive_fitting.py --demo                     # 用合成的 Ogden 数据演示
    python constitutive_fitting.py --data my.csv              # 拟合自己的数据
    python constitutive_fitting.py --data my.csv --out result # 自定义输出文件名前缀

输出：
    <out>_fit.png     拟合曲线 + 相对误差图
    <out>_params.csv  各模型拟合参数、RMSE、R^2

注意：所有参数与应力同单位；不可压单轴下 P(lambda) 公式见 06-知识库/本构模型速查.md
"""
import argparse
import csv
import os

import numpy as np
import matplotlib
matplotlib.use("Agg")  # 无界面环境也能出图
import matplotlib.pyplot as plt
from scipy.optimize import least_squares

# ----------------------------------------------------------------------
# 运动学：不可压单轴，lambda2 = lambda3 = lambda^(-1/2)
# ----------------------------------------------------------------------
def I1(lam):
    return lam ** 2 + 2.0 / lam

def I2(lam):
    return 2.0 * lam + 1.0 / lam ** 2

# ----------------------------------------------------------------------
# 各模型的名义应力 P(lambda)（不可压单轴）
# ----------------------------------------------------------------------
def p_neohooke(lam, C10):
    return 2.0 * C10 * (lam - lam ** -2)

def p_mooney_rivlin(lam, C10, C01):
    return 2.0 * (lam - lam ** -2) * (C10 + C01 / lam)

def p_yeoh(lam, C10, C20, C30):
    j = I1(lam) - 3.0
    return 2.0 * (lam - lam ** -2) * (C10 + 2.0 * C20 * j + 3.0 * C30 * j ** 2)

def p_gent(lam, mu, Jm):
    return mu * Jm * (lam - lam ** -2) / (Jm - (I1(lam) - 3.0))

def p_ogden(lam, *params):
    """params = (mu1, alpha1, mu2, alpha2, ...)，可任意项数"""
    s = np.zeros_like(lam, dtype=float)
    for i in range(0, len(params), 2):
        mu, alpha = params[i], params[i + 1]
        s = s + mu * (lam ** (alpha - 1.0) - lam ** (-alpha / 2.0 - 1.0))
    return s

def p_arruda_boyce(lam, mu, lamL):
    c = [1.0 / 2, 1.0 / 20, 11.0 / 1050, 19.0 / 7000, 519.0 / 673750]
    dpsi = 0.0
    for i in range(1, 6):
        dpsi = dpsi + c[i - 1] * i * lamL ** (2 - 2 * i) * I1(lam) ** (i - 1)
    return 2.0 * (lam - lam ** -2) * mu * dpsi

# 模型注册表：函数 / 参数初值 / 参数边界 / 参数名
MODELS = {
    "neo-Hookean":    dict(fn=p_neohooke,      p0=[15.0],                lb=[1e-6],       ub=[1e4],
                           names=["C10"]),
    "Mooney-Rivlin":  dict(fn=p_mooney_rivlin, p0=[12.0, 3.0],           lb=[1e-6, 0.0],  ub=[1e4, 1e4],
                           names=["C10", "C01"]),
    "Yeoh":           dict(fn=p_yeoh,          p0=[15.0, 2.0, 0.5],      lb=[1e-6, -1e3, -1e3], ub=[1e4, 1e3, 1e3],
                           names=["C10", "C20", "C30"]),
    "Gent":           dict(fn=p_gent,          p0=[15.0, 50.0],          lb=[1e-6, 1.0],  ub=[1e4, 1e4],
                           names=["mu", "Jm"]),
    "Ogden-2term":    dict(fn=p_ogden,         p0=[15.0, 2.0, 2.0, -2.0],
                           lb=[1e-6, -5.0, 1e-6, -5.0], ub=[1e4, 5.0, 1e4, 5.0],
                           names=["mu1", "alpha1", "mu2", "alpha2"]),
    "Arruda-Boyce":   dict(fn=p_arruda_boyce,  p0=[15.0, 3.0],           lb=[1e-6, 1.05], ub=[1e4, 10.0],
                           names=["mu", "lamL"]),
}

# ----------------------------------------------------------------------
# 拟合
# ----------------------------------------------------------------------
def fit_one(name, spec, lam, P):
    """用最小二乘拟合单个模型；残差做归一化，避免大应变主导"""
    fn = spec["fn"]

    def residual(theta):
        return (fn(lam, *theta) - P) / np.max(np.abs(P))

    try:
        res = least_squares(residual, x0=spec["p0"], bounds=(spec["lb"], spec["ub"]),
                            max_nfev=20000, xtol=1e-12, ftol=1e-12)
    except Exception as e:  # 拟合失败也要继续跑其他模型
        print(f"  [{name}] fit FAILED: {e}")
        return None

    P_hat = fn(lam, *res.x)
    rmse = float(np.sqrt(np.mean((P_hat - P) ** 2)))
    ss_res = float(np.sum((P - P_hat) ** 2))
    ss_tot = float(np.sum((P - np.mean(P)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return dict(name=name, params=dict(zip(spec["names"], res.x)), rmse=rmse, r2=r2,
                theta=res.x, fn=fn)


def load_csv(path):
    """读取两列 CSV（自动跳过表头 / 注释行 / 单位行）"""
    lam, P = [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if not row or len(row) < 2:
                continue
            head = row[0].strip().lower()
            if head.startswith(("stretch", "lambda", "λ", "#", "strain")) or not row[0][0:1].isdigit() and row[0][0:1] not in ".-":
                continue  # 表头/注释行
            try:
                l, s = float(row[0]), float(row[1])
            except ValueError:
                continue  # 单位行等
            lam.append(l); P.append(s)
    if len(lam) < 5:
        raise SystemExit(f"有效数据点太少（{len(lam)} 个），请检查 CSV 格式："
                         f"第1列 stretch(lambda)，第2列 stress(P)。")
    return np.array(lam), np.array(P)


def make_demo(path_demo):
    """合成一段带噪声的 Ogden(2-term) 单轴数据，单位 kPa"""
    rng = np.random.default_rng(42)
    lam = np.linspace(1.0, 4.0, 40)
    P_true = p_ogden(lam, 15.0, 2.0, 2.0, -2.0)
    P = P_true * (1 + rng.normal(0, 0.02, lam.size))
    P[0] = 0.0  # 无应变时应力为 0
    with open(path_demo, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stretch", "stress"])
        for l, s in zip(lam, P):
            w.writerow([f"{l:.4f}", f"{s:.4f}"])
    print(f"[demo] 已生成合成数据 -> {path_demo}（真实参数: Ogden mu1=15, alpha1=2, mu2=2, alpha2=-2 kPa）")


def plot_results(lam, P, fits, out_png):
    lam_fine = np.linspace(lam.min(), max(lam.max(), 1.05), 300)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2))

    ax1.plot(lam, P, "ko", ms=5, mfc="none", label="data")
    cmap = plt.cm.tab10
    for k, ft in enumerate(fits):
        ax1.plot(lam_fine, ft["fn"](lam_fine, *ft["theta"]), color=cmap(k), lw=1.6,
                 label=f"{ft['name']} (R²={ft['r2']:.4f})")
    ax1.set_xlabel("stretch $\\lambda$"); ax1.set_ylabel("nominal stress $P$ (kPa)")
    ax1.set_title("Uniaxial tension: constitutive fitting")
    ax1.legend(fontsize=8); ax1.grid(alpha=.3)

    for k, ft in enumerate(fits):
        rel = (ft["fn"](lam, *ft["theta"]) - P) / np.maximum(np.abs(P), 1e-12) * 100
        ax2.plot(lam, rel, "o-", ms=4, color=cmap(k), label=ft["name"])
    ax2.axhline(0, color="k", lw=.8)
    ax2.set_xlabel("stretch $\\lambda$"); ax2.set_ylabel("relative error (%)")
    ax2.set_title("Relative error of each model")
    ax2.grid(alpha=.3)

    fig.tight_layout()
    fig.savefig(out_png, dpi=160)
    print(f"[plot] 已保存 -> {out_png}")


def main():
    ap = argparse.ArgumentParser(description="Fit hyperelastic constitutive models to uniaxial data")
    ap.add_argument("--data", help="CSV 文件路径（stretch, stress）")
    ap.add_argument("--demo", action="store_true", help="生成并拟合合成 Ogden 数据")
    ap.add_argument("--out", default="fit_results", help="输出文件名前缀")
    args = ap.parse_args()

    if args.demo or not args.data:
        if not args.data:
            print("未指定 --data，自动使用演示模式（合成数据）。\n")
        demo_csv = args.out + "_demo_data.csv"
        make_demo(demo_csv)
        args.data = demo_csv

    lam, P = load_csv(args.data)
    print(f"已读入 {len(lam)} 个数据点，λ ∈ [{lam.min():.3f}, {lam.max():.3f}]，"
          f"P ∈ [{P.min():.3f}, {P.max():.3f}]\n")

    results = []
    print(f"{'model':<15}{'RMSE':>10}{'R^2':>10}   parameters")
    for name, spec in MODELS.items():
        ft = fit_one(name, spec, lam, P)
        if ft is None:
            continue
        results.append(ft)
        pstr = ", ".join(f"{k}={v:.4g}" for k, v in ft["params"].items())
        print(f"{name:<15}{ft['rmse']:>10.4f}{ft['r2']:>10.4f}   {pstr}")

    # 导出参数表
    out_csv = args.out + "_params.csv"
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["model", "RMSE", "R2", "parameters"])
        for ft in results:
            pstr = "; ".join(f"{k}={v:.6g}" for k, v in ft["params"].items())
            w.writerow([ft["name"], f"{ft['rmse']:.5f}", f"{ft['r2']:.5f}", pstr])
    print(f"\n[table] 已保存 -> {out_csv}")

    plot_results(lam, P, results, args.out + "_fit.png")
    print("\n提示：R^2 高不代表模型物理合理！注意参数个数（过拟合）与外推行为，"
          "建议结合多变形模式（单轴/等双轴/剪切）联合拟合。")


if __name__ == "__main__":
    main()
