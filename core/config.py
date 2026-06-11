"""全局配置：LLM、模型映射、撮合规则、调度参数。"""
from __future__ import annotations
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "battle.db"

# ============ LLM ============
# 默认仍使用 CodeBuddy OpenAI-compatible endpoint。
LLM_API_KEY = os.getenv("CODEBUDDY_API_KEY", "").strip()
LLM_ENDPOINTS = [
    os.getenv("CODEBUDDY_ENDPOINT", "").strip() or None,
    "https://www.codebuddy.cn/v2/chat/completions",
    "http://auth.proxy/codebuddy/v2/chat/completions",
]
LLM_ENDPOINTS = [e for e in LLM_ENDPOINTS if e]

# 默认模型（作为全局兜底）
DEFAULT_MODEL = os.getenv("MODEL_DEFAULT", "gpt-5.5")

# 向后兼容：保留旧变量，同时可通过更细粒度 MODEL_* 覆盖。
LLM_MODEL = os.getenv("LLM_MODEL", DEFAULT_MODEL)             # CIO 大脑
EXPERT_MODEL = os.getenv("EXPERT_MODEL", "gpt-5.4")         # 金融专家
NEWS_MODEL = os.getenv("NEWS_MODEL", "deepseek-v4")         # 新闻/情绪类
MARKET_MODEL = os.getenv("MARKET_MODEL", "gpt-5.1")         # 基本面/技术类

def _base_agent_models() -> dict[str, str]:
    return {
        "cio_brain": LLM_MODEL,
        "financial_expert": EXPERT_MODEL,
        "daily_reflection": EXPERT_MODEL,
        "news_worker": NEWS_MODEL,
        "news_analyst": NEWS_MODEL,
        "sentiment_analyst": NEWS_MODEL,
        "fundamentals_analyst": MARKET_MODEL,
        "technical_analyst": MARKET_MODEL,
    }


AGENT_MODEL_CONFIG_FILE = Path(
    os.getenv("AGENT_MODEL_CONFIG_FILE", str(ROOT / "agent_models.json"))
)

# 子 agent 模型映射（支持文件 + 环境变量 + JSON 覆盖）
AGENT_MODELS = _base_agent_models()

if AGENT_MODEL_CONFIG_FILE.exists():
    try:
        _file_cfg = json.loads(AGENT_MODEL_CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(_file_cfg, dict):
            for k, v in _file_cfg.items():
                if isinstance(k, str) and isinstance(v, str) and v.strip():
                    AGENT_MODELS[k.strip()] = v.strip()
    except Exception:
        # 文件格式错误时静默忽略，避免启动失败。
        pass


_ENV_AGENT_KEYS = {
    "MODEL_CIO_BRAIN": "cio_brain",
    "MODEL_FINANCIAL_EXPERT": "financial_expert",
    "MODEL_DAILY_REFLECTION": "daily_reflection",
    "MODEL_NEWS_WORKER": "news_worker",
    "MODEL_NEWS_ANALYST": "news_analyst",
    "MODEL_SENTIMENT_ANALYST": "sentiment_analyst",
    "MODEL_FUNDAMENTALS_ANALYST": "fundamentals_analyst",
    "MODEL_TECHNICAL_ANALYST": "technical_analyst",
}

for env_key, agent_key in _ENV_AGENT_KEYS.items():
    val = os.getenv(env_key, "").strip()
    if val:
        AGENT_MODELS[agent_key] = val

_MODEL_OVERRIDES_RAW = os.getenv("MODEL_OVERRIDES_JSON", "").strip()
if _MODEL_OVERRIDES_RAW:
    try:
        _ov = json.loads(_MODEL_OVERRIDES_RAW)
        if isinstance(_ov, dict):
            for k, v in _ov.items():
                if isinstance(k, str) and isinstance(v, str) and v.strip():
                    AGENT_MODELS[k.strip()] = v.strip()
    except Exception:
        # 覆盖 JSON 非法时静默忽略，避免启动失败。
        pass


def get_model(agent_name: str, fallback: str | None = None) -> str:
    """按 agent 名称返回模型；找不到时回退到 fallback 或全局默认。"""
    if not agent_name:
        return fallback or DEFAULT_MODEL
    return AGENT_MODELS.get(agent_name, fallback or DEFAULT_MODEL)

LLM_TIMEOUT = 30
LLM_MAX_RETRY = 2

# ============ 资金 ============
INITIAL_CAPITAL = 100_000.0
COMMISSION_RATE = 0.00025      # 万 2.5
STAMP_TAX_RATE = 0.0005        # 卖出印花税
SLIPPAGE_BPS = 5               # 5 个基点滑点
MIN_TRADE_AMOUNT = 1_000.0     # 最小成交额

# ============ 调度 ============
TICK_INTERVAL_MIN = int(os.getenv("TICK_INTERVAL_MIN", "5"))   # 每 5 分钟一轮（含非交易时段）
ALWAYS_ON = os.getenv("ALWAYS_ON", "0") == "1"   # 默认 0：股市闭市时不动作
# A 股交易时段
TRADING_SESSIONS = [("09:30", "11:30"), ("13:00", "15:00")]

# ============ 股票池 ============
# 观察池 = "起点候选"，CIO 应使用 search_stocks/market_top 从全 A 股 5500 只继续扩展。
# 当前主线偏向科技/AI/算力/机器人，观察池倾斜成长方向（约 50 只）。
WATCHLIST = [
    # ===== 科技 / AI / 半导体 / CPO（主线 ~40%）=====
    "000063",  # 中兴通讯
    "002475",  # 立讯精密
    "300308",  # 中际旭创
    "300502",  # 新易盛
    "601138",  # 工业富联
    "000725",  # 京东方A
    "603986",  # 兆易创新
    "688981",  # 中芯国际
    "688008",  # 澜起科技
    "688041",  # 海光信息
    "688256",  # 寒武纪
    "002230",  # 科大讯飞
    "300223",  # 北京君正
    "603501",  # 韦尔股份
    "002241",  # 歌尔股份
    "002371",  # 北方华创
    "603160",  # 汇顶科技
    "300433",  # 蓝思科技
    # ===== 新能源 / 电池 / 机器人 =====
    "300750",  # 宁德时代
    "002594",  # 比亚迪
    "300274",  # 阳光电源
    "601012",  # 隆基绿能
    "300124",  # 汇川技术（机器人）
    "002747",  # 埃斯顿（机器人）
    "300024",  # 机器人
    "002180",  # 纳川股份
    # ===== 医药生物（成长）=====
    "600276",  # 恒瑞医药
    "300760",  # 迈瑞医疗
    "603259",  # 药明康德
    "300347",  # 泰格医药
    # ===== 消费 =====
    "600519",  # 贵州茅台
    "000858",  # 五粮液
    "000333",  # 美的集团
    "600887",  # 伊利股份
    # ===== 金融保险（防守底仓，~15%）=====
    "600036",  # 招商银行
    "601318",  # 中国平安
    "000001",  # 平安银行
    "300059",  # 东方财富
    "600030",  # 中信证券
    # ===== 资源 / 红利 =====
    "601899",  # 紫金矿业
    "601088",  # 中国神华
    "600900",  # 长江电力
    # ===== 国防军工 =====
    "600760",  # 中航沈飞
]
# 去重保险
WATCHLIST = list(dict.fromkeys(WATCHLIST))
