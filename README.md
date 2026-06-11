# Quant Agent Battle (API-Free Edition)

一个面向 A 股模拟交易的多 Agent 系统，去除了 Web API 服务层，保留核心策略引擎、调度器、风控、机会捕手、反思与长期记忆。

## What Changed

- 已删除 API 服务相关代码：`api/`、Web 仪表盘入口
- 运行方式改为本地调度脚本：`scripts/run_scheduler.py`
- 保留并强化 LLM 配置：默认走 CodeBuddy endpoint，每个子 agent 模型可独立配置
- 移除默认硬编码 API Key，必须通过环境变量注入

## Project Structure

- `core/`：行情、LLM、数据库、风控、机会捕手、反思、工具集
- `orchestrator/`：CIO/新闻/行情 worker
- `scripts/run_scheduler.py`：定时调度入口（无 HTTP API）
- `scripts/reset_account.py`：重置模拟账户
- `data/`：缓存与状态数据
- `logs/`：日志目录

## Quick Start

## 1) Install

```bash
pip install -r requirements.txt
```

## 2) Configure Environment

最少只需配置 CodeBuddy Key：

```bash
# Windows PowerShell
$env:CODEBUDDY_API_KEY="your_codebuddy_key"
```

可选自定义 endpoint（默认已内置 CodeBuddy 官方地址）：

```bash
$env:CODEBUDDY_ENDPOINT="https://www.codebuddy.cn/v2/chat/completions"
```

## 3) Run Scheduler

在项目根目录执行：

```bash
python scripts/run_scheduler.py
```

启动后将自动调度：

- `quote_worker`：每 5 分钟
- `guardian_scan`：每 5 分钟（错峰）
- `opportunity_scan`：每 5 分钟（错峰）
- `news_worker`：每 15 分钟
- `brain_worker`：每小时
- `run_daily_reflection`：工作日 15:10

## 4) Reset Account (Optional)

```bash
python -m scripts.reset_account
```

如果你的环境是包模式（外层有统一包名），也可以继续用原来的包路径调用 reset 脚本。

## Model Configuration (Per Agent)

默认仍使用 CodeBuddy API，模型可以按子 agent 定制。

### Global Defaults

- `MODEL_DEFAULT`：全局默认模型
- `LLM_MODEL`：CIO 主模型（兼容旧变量）
- `EXPERT_MODEL`：专家模型（兼容旧变量）
- `NEWS_MODEL`：新闻/情绪默认模型（兼容旧变量）
- `MARKET_MODEL`：基本面/技术默认模型（兼容旧变量）

### Per-Agent Variables (Recommended)

- `MODEL_CIO_BRAIN`
- `MODEL_FINANCIAL_EXPERT`
- `MODEL_DAILY_REFLECTION`
- `MODEL_NEWS_WORKER`
- `MODEL_NEWS_ANALYST`
- `MODEL_SENTIMENT_ANALYST`
- `MODEL_FUNDAMENTALS_ANALYST`
- `MODEL_TECHNICAL_ANALYST`

### JSON Bulk Override (Optional)

也可以一次性覆盖多个子 agent：

```bash
$env:MODEL_OVERRIDES_JSON='{"news_worker":"gpt-5.1","technical_analyst":"deepseek-v4"}'
```

## Example Config (PowerShell)

```bash
$env:CODEBUDDY_API_KEY="your_codebuddy_key"
$env:MODEL_CIO_BRAIN="gpt-5.5"
$env:MODEL_FINANCIAL_EXPERT="gpt-5.4"
$env:MODEL_NEWS_WORKER="deepseek-v4"
$env:MODEL_NEWS_ANALYST="deepseek-v4"
$env:MODEL_SENTIMENT_ANALYST="deepseek-v4"
$env:MODEL_FUNDAMENTALS_ANALYST="gpt-5.1"
$env:MODEL_TECHNICAL_ANALYST="gpt-5.1"
python scripts/run_scheduler.py
```

## Notes

- 不设置 `CODEBUDDY_API_KEY` 会导致 LLM 调用失败。
- `ALWAYS_ON=0` 时，仅工作日 09:00-16:00 执行核心任务；`ALWAYS_ON=1` 可强制全天运行。
- 数据与交易记录写入 SQLite（`data/` 下）。

## Security

- 仓库不再包含默认密钥。
- 建议用环境变量或本地密钥管理工具注入密钥，不要写入代码或提交到 Git。
