"""推荐排序引擎。

分层混合架构：
- 粗排：候选池过滤
- 精排：个性化匹配分（topic / lang / author / star_range / novelty / trend）
- 重排：MMR 多样性（λ=0.7）

冷启动：被动模式 + 主动引导问卷 + 可选 GitHub OAuth

参考：docs/algorithm-recommendation.md
"""
