"""兴趣模型构建与更新。

职责：
- 维护 topics / languages / authors 兴趣画像
- 增量更新（EMA α=0.3）
- 兴趣漂移检测（JS 散度阈值 0.15）

参考：docs/algorithm-personalized-memory.md
存储：data/profile/interests.json
"""
