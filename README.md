<p align="center">
  <img src="static/readme/hero.svg" width="100%" alt="StarRadar 星探 · 看见下一颗明星 · GitHub 潜力项目发现雷达">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/在线体验-GitHub%20Pages-1677ff" alt="在线体验">
  <img src="https://img.shields.io/badge/Python-3.10%2B-1677ff" alt="Python">
  <img src="https://img.shields.io/badge/前端-原生JS零框架-1677ff" alt="前端">
  <img src="https://img.shields.io/badge/自动化-每日06_00-28a86b" alt="自动化">
  <img src="https://img.shields.io/badge/AI-中文解读-f5a623" alt="AI 解读">
  <img src="https://img.shields.io/badge/测试-143%20passed-28a86b" alt="测试">
</p>

---

## 定位

GitHub Trending 上榜时，热度红利早已被瓜分。StarRadar 反着来——专挑 **50 ~ 5000 星**的「中量级潜力股」，用增长动能量化「谁正在起飞」，而不是「谁已经最大」。

| 传统热门榜 | StarRadar |
| --- | --- |
| 按 Star 总数排序，巨无霸霸榜 | 按**增长动能**排序，新面孔优先 |
| 只告诉你「它火了」 | 告诉你「它**为什么值得关注**」（AI 解读） |
| 千人一面 | 问卷 + 行为记忆 + 个性化精选 |

> **核心理念**：星数无意义，增长动能才上榜。跑不赢同规模常态增速的巨无霸，进不了 StarRadar 的榜。

---

## 界面

<p align="center">
  <img src="static/screenshots/shot-hero.png" alt="StarRadar 首页" width="780">
  <br>
  <sub>首页 · 今日星图 + 潜力榜单</sub>
</p>

---

## 核心能力

| 能力 | 说明 |
| --- | --- |
| **每日潜力雷达** | 3 个 Star 区间（50-200 / 200-1000 / 1000-5000）多桶采样，过滤课程 / 课件 / 镜像噪声；五维评分：速度 · 加速度 · 社区健康 · 新鲜度 · 信号，动态基准对比同规模项目 |
| **每周趋势周报** | TrendScore v2 增长动能排序：growth 40% + novelty 20% + accel 15% + excess 10% + topic 10% + health 5%；增速门票制，巨无霸不进榜；新星 / 热度 TOP / 领域走势 / 我的关注四大板块，跨周追踪 + 状态徽章 |
| **为你精选** | 40 个中文标签问卷 → 冷启动画像 → topic 匹配 + MMR 多样性重排 → 推荐理由；行为 EMA（α=0.3）增量更新 + 遗忘曲线，越用越准 |
| **AI 中文解读** | 每个上榜项目生成一段「为什么值得关注」中文解读，增量缓存（星数变化 >20% 才重读），失败自动降级规则文本 |
| **GitHub 原生集成** | 一键登录（PAT / OAuth 设备流 / 跳转登录）→ 网页内直接加星 · Fork · 复制克隆命令 · 随行笔记 |
| **全自动管线** | 每日 06:00：采集 → 评分 → 解读 → 快照落库 → 部署 Pages；每周一 08:00 周报自动上线 |

---

## 五维潜力评分

<p align="center">
  <img src="static/readme/radar-5d.svg" width="100%" alt="StarRadar 五维潜力评分：速度、加速度、健康、新鲜、信号（示例数据来自 tsingyuai/growth-lab）">
</p>

五个维度都以**同规模项目的动态基准**为参照，而非绝对值——「正在起飞」的中量级项目，评分可以高过百倍体量的老牌仓库。

---

## 个性化闭环

数据流：浏览器（localStorage）→ 本地 `--serve` 落库（SQLite）→ 每日管道重算画像 → 生成精选。

平时只开 GitHub Pages 也没关系：行为会积累在本机浏览器，本地跑一次 `--serve` 打开页面即自动同步（footer「同步数据」按钮），问卷改动也会自动补报进后端。

---

## 快速开始

```bash
git clone https://github.com/2BingLing/StarRadar.git
cd StarRadar
pip install -r requirements.txt
cp .env.example .env        # GITHUB_TOKEN 必填；LLM_* 可选（DeepSeek 等 OpenAI 兼容端点）

python src/main.py --serve  # 本地观测台 → http://127.0.0.1:8970/
python src/main.py          # 每日管道：采集 → 评分 → 画像 → 解读 → 生成 JSON
python src/main.py --weekly # 生成 / 刷新每周趋势周报
pytest                      # 143 passed
```

---

## 目录 / 技术栈

| 层 | 内容 |
| --- | --- |
| src/ | collector（采集）· analyzer（评分）· profile（画像 / 推荐）· reporter（周报 / LLM 解读）· search（语义搜索）· web（本地服务 + OAuth） |
| static/ | GitHub Pages 前端（原生 JS，零框架）：index.html · js/ · css/ · data/（CI 生成 JSON） |
| data/profile/ | memory.db（问卷 + 行为 + 快照）+ interests.json（兴趣画像） |
| .github/ | daily.yml 每日刷新 · weekly.yml 周一周报 · Pages 部署 |
| 后端 | Python 3.10+ · SQLite · numpy · scikit-learn · BGE 嵌入 · HNSW · BM25 |
| 自动化 | GitHub Actions（公开仓库免费无限分钟） |

---

<p align="center">
  <img src="static/screenshots/shot-potential.png" alt="潜力雷达全页" width="780">
  <br>
  <sub>潜力雷达 · 全页</sub>
</p>

---

## License

[GNU Affero General Public License v3.0](LICENSE) — 允许自由使用与修改，但**衍生作品及基于本项目的网络服务必须同样以 AGPL 开源**。

---

<p align="center">
  <img src="static/starlogo.png" alt="StarRadar" width="40">
  <br>
  <sub>以每周节奏更新你的私人星图 · StarRadar</sub>
</p>
