# 机器学习 × 力学：经典文献清单

> 顺序即推荐阅读顺序。评分栏读完后再填。

## 奠基与综述

1. Raissi, Perdikaris & Karniadakis (2019). Physics-informed neural networks: A deep learning framework for solving forward and inverse problems involving nonlinear partial differential equations. *J. Comput. Phys.* 378, 686–707.
   —— PINN 开山之作。
2. Karniadakis et al. (2021). Physics-informed machine learning. *Nat. Rev. Phys.* 3, 422–440.
   —— 全景综述：正向/反向/算子学习一条线讲清。
3. Lu, Jin, Pang, Zhang & Karniadakis (2021). Learning nonlinear operators via DeepONet based on the universal approximation theorem of operators. *Nat. Mach. Intell.* 3, 218–229.
4. Li et al. (2021). Fourier neural operator for parametric partial differential equations. *ICLR 2021*.
   —— FNO，算子学习的另一大流派。

## 数据驱动本构（与软物质最相关）

5. Linka & Kuhl (2023). A new family of constitutive artificial neural networks towards automated model discovery. *CMAME* 403, 115731.
   —— CAN：可解释的本构网络设计范式。
6. Thakolkaran, Joshi, Shen, Kumar & Kochmann (2022). NN-EUCLID: Deep-learning hyperelasticity without stress data. *JMPS* 169, 105076.
   —— 只有全位移场（DIC 数据类型）也能发现超弹性能。
7. As'ad, Avery & Farhat (2022). A mechanics-informed artificial neural network approach in data-driven constitutive modeling. *Int. J. Numer. Meth. Eng.* 123, 2738–2759.
   —— 输入不变量保证客观性的代表工作。
8. 教材：Brunton & Kutz《Data-Driven Science and Engineering》2nd ed.（配套免费视频课）。

## 实战避坑

9. Krishnapriyan et al. (2021). Characterizing possible failure modes in physics-informed neural networks. *NeurIPS 2021*.
   —— PINN 什么时候会失败：调度/权重问题的系统分析，做研究前必读。
10. Wang, Yu & Perdikaris (2022). When and why PINNs fail to train: A neural tangent kernel perspective. *J. Comput. Phys.* 449, 110768.
    —— 从优化理论解释 PINN 训练困难。

## 检索关键词（保持更新）

- physics-informed neural network + (hyperelastic | viscoelastic | gel | swelling)
- neural constitutive model / constitutive discovery / model-free mechanics
- operator learning + solid mechanics; inverse problem + full-field measurement (DIC)
- data-driven + fracture / fatigue + soft materials

> 维护方式：每月补 2–3 篇新论文并写一句话理由；过时/被超越的工作移到"历史脉络"小节，别删（了解演化史本身有价值）。
