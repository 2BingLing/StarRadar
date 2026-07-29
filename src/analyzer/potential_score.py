"""潜力分计算器。

综合评分维度（设计文档第 4 章）：
- 星速 S_vel（w=0.30）
- 加速度 S_acc（w=0.25）
- 社区健康 S_health（w=0.20）
- 新鲜度 S_fresh（w=0.15）
- 信号质量 S_signal（w=0.10）

算法增强（参见 docs/algorithm-potential-score.md）：
- 几何平均聚合（避免单维度拉高）
- Gompertz 增长曲线拟合
- Kleinberg 爆发点检测
- Wilson Score / 贝叶斯估计处理小样本
"""
