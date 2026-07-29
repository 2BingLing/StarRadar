# StarRadar · 星探

> 中文 AI 驱动的 GitHub 潜力项目发现周报 — 不只追踪热门，更发现明日之星。

## 简介

StarRadar 是一个自动发现 GitHub 潜力项目的工具：

- **综合潜力评分**：不只看 Stars/天，而是综合速度、加速度、社区健康度、新鲜度、信号质量
- **潜力雷达**：专门发现 500-5000 stars、正在加速增长的小众项目
- **AI 中文解读**：每个项目配中文摘要、亮点、适合谁用
- **个性化记忆**：记录你的兴趣，越用越懂你
- **语义搜索**：混合检索（BM25 + 向量）+ Cross-Encoder 重排
- **GitHub 集成**：一键加星 / Fork / 克隆

## 技术栈

- **语言**：Python 3.11+
- **CI/CD**：GitHub Actions（每周一自动生成周报）
- **托管**：GitHub Pages
- **LLM**：OpenAI / Claude
- **检索**：BGE 嵌入 + HNSW + BM25 + Cross-Encoder

## 目录结构

```
starradar/
├── .github/workflows/     # GitHub Actions 调度
├── src/
│   ├── main.py            # 入口
│   ├── collector/         # 数据采集
│   ├── analyzer/          # 潜力评分 / 分类 / 趋势
│   ├── profile/           # 个性化记忆 / 推荐
│   ├── search/            # 语义搜索
│   ├── reporter/          # LLM 摘要生成
│   └── publisher/         # HTML 渲染 / 邮件发送
├── static/                # 前端静态资源
├── tests/                 # 测试
├── data/                  # 运行时数据（缓存 + 用户档案）
├── output/                # 生成产物（gh-pages）
├── config.py              # 全局配置
└── requirements.txt
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env  # 然后填入 GITHUB_TOKEN / LLM_API_KEY

# 运行
python src/main.py
```

## License

MIT
