"""数据库层：Python 标准库 sqlite3 + 数据库级硬约束，不引入 ORM 与任何中间件。"""

import json                                                    # 序列化版本快照与调研会话内容
import sqlite3                                                  # 标准库 SQLite 驱动，零外部依赖
import uuid                                                     # 生成版本、复核、会话等主键
from contextlib import contextmanager                          # 实现事务/连接上下文管理
from datetime import datetime, timezone                       # 生成 UTC 时间戳，避免本地时区造成审计歧义
from pathlib import Path                                        # 创建数据目录
from typing import Any, Iterator                               # 类型标注

from app.config import resolve_path                            # 把配置里的相对路径解析为绝对路径

# 建表语句：每一行都带 SQL 注释，便于 DBA 逐行复核约束设计
_SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (                          -- 员工主档，只存脱敏信息
    emp_id      TEXT PRIMARY KEY,                              -- 员工工号，业务主键
    name_mask   TEXT NOT NULL,                                 -- 脱敏姓名（如 张*），禁止存明文姓名与证件号
    dept        TEXT NOT NULL,                                 -- 所属部门
    grade       TEXT NOT NULL,                                 -- 职级，调研结论需引用
    created_at  TEXT NOT NULL                                  -- 创建时间（UTC ISO8601）
);                                                             -- 员工表结束

CREATE TABLE IF NOT EXISTS salary_records (                    -- 当前生效薪酬，永远只有一行/人
    emp_id            TEXT PRIMARY KEY,                        -- 员工工号
    base_salary_cents INTEGER NOT NULL,                        -- 月薪，单位为分，整数避免浮点误差
    currency          TEXT NOT NULL DEFAULT 'CNY',             -- 币种，默认人民币
    version_no        INTEGER NOT NULL,                        -- 乐观锁版本号，生效时用于检测并发覆盖
    updated_at        TEXT NOT NULL                            -- 最后生效时间（UTC ISO8601）
);                                                             -- 薪酬表结束

CREATE TABLE IF NOT EXISTS salary_change_versions (           -- 变更版本链，原记录永不被覆盖
    version_id         TEXT PRIMARY KEY,                       -- 版本 UUID
    emp_id             TEXT NOT NULL,                          -- 关联员工
    from_version_no    INTEGER NOT NULL,                       -- 基于哪个薪酬版本号变更
    before_json        TEXT NOT NULL,                          -- 变更前快照（JSON），用于差异对比
    after_json         TEXT NOT NULL,                          -- 变更后内容（JSON）
    reason             TEXT NOT NULL,                          -- 变更事由，必须书面留痕
    research_question  TEXT NOT NULL DEFAULT '',               -- 现场调研的核心问题，提交时随版本固化
    operator_id        TEXT NOT NULL,                          -- 经办人 ID，双人复核时用于排除本人
    status             TEXT NOT NULL,                          -- 状态机取值见 STATUS_* 常量
    research_session_id TEXT,                                  -- 关联的现场调研会话 ID
    created_at         TEXT NOT NULL,                          -- 创建时间
    submitted_at       TEXT,                                   -- 提交复核时间
    effective_at       TEXT,                                   -- 生效时间
    CHECK (status IN ('DRAFT','RESEARCHING','RESEARCH_DONE','PENDING_REVIEW','REVIEW_ONE','APPROVED','EFFECTIVE','REJECTED'))  -- 数据库层锁死状态取值
);                                                             -- 版本表结束

CREATE TABLE IF NOT EXISTS review_records (                    -- 双人复核轨迹，一人一笔
    review_id   TEXT PRIMARY KEY,                              -- 复核记录 UUID
    version_id  TEXT NOT NULL,                                 -- 关联变更版本
    reviewer_id TEXT NOT NULL,                                 -- 复核人 ID
    slot        INTEGER NOT NULL,                              -- 第 1 / 第 2 复核位，最多两个
    decision    TEXT NOT NULL,                                 -- APPROVE 或 REJECT
    comment     TEXT NOT NULL DEFAULT '',                      -- 复核意见，书面留痕
    created_at  TEXT NOT NULL,                                 -- 复核时间
    UNIQUE (version_id, slot),                                -- 硬约束：同一个版本最多两个复核位
    UNIQUE (version_id, reviewer_id),                         -- 硬约束：同一个人不能复核两次
    CHECK (slot IN (1,2)),                                    -- 硬约束：复核位只能是 1 或 2
    CHECK (decision IN ('APPROVE','REJECT')),                 -- 硬约束：结论只有通过与驳回
    FOREIGN KEY (version_id) REFERENCES salary_change_versions(version_id)  -- 保证复核记录不能孤立存在
);                                                             -- 复核表结束

CREATE TRIGGER IF NOT EXISTS trg_review_no_self                -- 数据库触发器：把"自己批自己"卡在写入层
BEFORE INSERT ON review_records                               -- 在插入复核记录之前执行
BEGIN                                                         -- 触发器体开始
    SELECT CASE                                               -- 用 CASE 判断是否为同一人
        WHEN NEW.reviewer_id = (                              -- 新复核人等于该版本经办人时
            SELECT operator_id FROM salary_change_versions    -- 从版本表查出经办人
            WHERE version_id = NEW.version_id                 -- 按本次复核的版本定位
        )                                                     -- 子查询结束
        THEN RAISE(ABORT, 'REVIEW_SAME_PERSON')               -- 直接中止事务，绕过服务层也拦得住
    END;                                                      -- CASE 结束
END;                                                          -- 触发器结束

CREATE TABLE IF NOT EXISTS research_sessions (               -- 现场调研会话
    session_id      TEXT PRIMARY KEY,                         -- 会话 UUID
    version_id      TEXT NOT NULL,                            -- 关联变更版本
    status          TEXT NOT NULL,                            -- WAIT_CONFIRM / CONFIRMED / FAILED
    question        TEXT NOT NULL,                            -- 本场调研的核心问题
    transcript_json TEXT NOT NULL,                            -- 多轮工具调用记录（JSON 数组）
    conclusion_json TEXT,                                     -- 模型输出的结构化结论，未确认前不生效
    confirmed_by    TEXT,                                     -- 人工确认人 ID
    created_at      TEXT NOT NULL,                            -- 创建时间
    confirmed_at    TEXT,                                     -- 人工确认时间
    FOREIGN KEY (version_id) REFERENCES salary_change_versions(version_id)  -- 会话必须挂在一个版本上
);                                                            -- 调研会话表结束

CREATE TABLE IF NOT EXISTS kb_docs (                          -- 知识库原文，制度文档的落地表
    doc_id     TEXT PRIMARY KEY,                              -- 文档 UUID
    title      TEXT NOT NULL,                                 -- 文档标题
    content    TEXT NOT NULL,                                 -- 文档正文，供全文检索
    source     TEXT NOT NULL,                                 -- 来源标识，便于审计引用
    created_at TEXT NOT NULL                                  -- 入库时间
);                                                            -- 知识库表结束

CREATE TABLE IF NOT EXISTS audit_log (                       -- 审计流水，哈希链防篡改
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,           -- 严格递增序号，链的顺序依据
    ts           TEXT NOT NULL,                               -- 事件时间
    actor_id     TEXT NOT NULL,                               -- 操作人 ID
    action       TEXT NOT NULL,                               -- 动作名，如 CHANGE_CREATE / REVIEW_APPROVE
    target       TEXT NOT NULL,                               -- 操作对象 ID
    payload_hash TEXT NOT NULL,                               -- 载荷摘要，只存哈希不存明文金额
    prev_hash    TEXT NOT NULL,                               -- 上一条记录的哈希
    row_hash     TEXT NOT NULL                                -- 本条记录哈希，串联成链
);                                                            -- 审计表结束
"""

# 状态机常量：集中定义，服务层与验证脚本共用，避免各处硬编码字符串
STATUS_DRAFT = "DRAFT"                                         # 草稿：刚创建，尚未调研
STATUS_RESEARCHING = "RESEARCHING"                             # 调研中：harness 正在跑
STATUS_RESEARCH_DONE = "RESEARCH_DONE"                         # 调研结论已人工确认，可提交复核
STATUS_PENDING_REVIEW = "PENDING_REVIEW"                       # 待第一人复核
STATUS_REVIEW_ONE = "REVIEW_ONE"                               # 第一人已通过，待第二人复核
STATUS_APPROVED = "APPROVED"                                   # 双人通过，待管理员生效
STATUS_EFFECTIVE = "EFFECTIVE"                                 # 已生效，薪酬记录已更新
STATUS_REJECTED = "REJECTED"                                   # 被驳回，不可再流转


def now_utc() -> str:
    """生成 UTC ISO8601 时间戳。

    入参:
        无。
    出参:
        形如 2026-11-12T06:30:00.123456+00:00 的字符串。
    失败时:
        系统时钟异常时抛 OSError，由上层兜底异常处理器转成 500。
    """
    return datetime.now(timezone.utc).isoformat()               # 统一 UTC，跨时区审计不会错序


def new_id(prefix: str) -> str:
    """生成带业务前缀的唯一 ID。

    入参:
        prefix: 前缀字符串，如 ver / rev / ses / doc。
    出参:
        prefix + UUID4 十六进制串。
    失败时:
        uuid4 依赖系统随机源，极端情况下抛异常并向上冒泡。
    """
    return f"{prefix}_{uuid.uuid4().hex}"                       # UUID4 保证跨进程不冲突


def connect(db_path: str) -> sqlite3.Connection:
    """建立数据库连接。

    入参:
        db_path: SQLite 文件路径，来自 config.yaml 的 db.path。
    出参:
        sqlite3.Connection，row_factory 已设为 Row，外键已开启。
    失败时:
        目录不存在或无权限时抛 sqlite3.OperationalError，调用方不应吞掉。
    """
    from app.config import load_config                    # 延迟导入，避免配置模块与数据库模块循环依赖
    busy_ms = int(load_config()["db"]["busy_timeout_ms"])  # 锁等待时间来自配置，不在代码里写死
    p = Path(db_path)                                          # 转成 Path 便于创建父目录
    p.parent.mkdir(parents=True, exist_ok=True)                # 父目录不存在时自动创建
    conn = sqlite3.connect(str(p), timeout=busy_ms / 1000.0, isolation_level=None)  # 手动控制事务
    conn.row_factory = sqlite3.Row                             # 让查询结果可按键名访问
    conn.execute("PRAGMA foreign_keys = ON")                   # 必须显式开启外键，SQLite 默认关闭
    conn.execute(f"PRAGMA busy_timeout = {busy_ms}")           # PRAGMA 不支持参数绑定，值已强转为 int，安全
    return conn                                                # 返回可用连接


@contextmanager                                                 # 声明为上下文管理器
def session(db_path: str) -> Iterator[sqlite3.Connection]:
    """请求级数据库连接上下文：退出时必定关闭。

    入参:
        db_path: SQLite 文件路径。
    出参:
        迭代出 sqlite3.Connection。
    失败时:
        业务代码抛异常时仍执行 finally 关闭连接，不吞异常、不提交半成品事务。
    """
    conn = connect(db_path)                                     # 建立连接
    try:                                                        # 保护业务代码块
        yield conn                                              # 把连接交给调用方
    finally:                                                    # 无论成功失败都要释放
        conn.close()                                            # 关闭连接，避免文件句柄泄漏


@contextmanager                                                 # 声明为上下文管理器
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务：BEGIN IMMEDIATE 抢写锁，保证双人复核的并发安全。

    入参:
        conn: 已建立的数据库连接。
    出参:
        迭代出同一连接，供 with 块内执行 SQL。
    失败时:
        任何异常都 ROLLBACK 并原样向上抛出，绝不留下半写入状态。
    """
    conn.execute("BEGIN IMMEDIATE")                             # 立即抢写锁，杜绝两人同时读到 REVIEW_ONE 后都当第二人
    try:                                                        # 保护事务内的全部写操作
        yield conn                                              # 交还连接
        conn.execute("COMMIT")                                  # 全部成功后一次性提交
    except Exception:                                          # 任意失败（含数据库约束触发器中止）
        conn.execute("ROLLBACK")                               # 回滚到事务开始前
        raise                                                  # 原样抛出，由全局异常处理器统一封装


def _ensure_fts(conn: sqlite3.Connection, tokenizer: str) -> bool:
    """创建 FTS5 全文索引表，不支持 trigram 时降级为普通检索。

    入参:
        conn: 数据库连接。
        tokenizer: 配置中的分词器名称，通常是 trigram（支持中文子串）。
    出参:
        True 表示 FTS5 可用；False 表示已降级，后续查询走 LIKE。
    失败时:
        建表报 OperationalError 时返回 False，不影响主流程启动。
    """
    ddl = (                                                     # 拼装虚表语句，doc_id 不参与索引
        "CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts "
        f"USING fts5(doc_id UNINDEXED, title, content, tokenize='{tokenizer}')"
    )                                                          # 语句拼装结束
    try:                                                        # 某些 SQLite 编译版本没有 trigram 分词器
        conn.execute(ddl)                                       # 尝试创建虚表
        return True                                            # 成功则启用全文检索
    except sqlite3.OperationalError:                            # 分词器不可用或 FTS5 未编译
        return False                                            # 降级标记，kb.search 会改用 LIKE


def init_db(cfg: dict) -> dict:
    """初始化数据库：建表、建触发器、建全文索引、写入种子数据。

    入参:
        cfg: 全局配置字典，读取 db.path / db.seed_employee / kb 段。
    出参:
        {"db_path": 绝对路径, "fts": bool, "seeded": bool}，供 /health 展示。
    失败时:
        建表失败抛 sqlite3.Error，服务启动失败；重复调用是幂等的，不会清空已有数据。
    """
    db_path = str(resolve_path(cfg["db"]["path"]))             # 解析数据库绝对路径
    with session(db_path) as conn:                             # 用上下文确保连接关闭
        conn.executescript(_SCHEMA)                            # 执行全部 DDL 与触发器，IF NOT EXISTS 保证幂等
        fts = _ensure_fts(conn, cfg["kb"]["fts_tokenizer"])    # 建立全文索引，失败则降级
        seeded = False                                         # 记录本次是否写入了种子数据
        emp = cfg.get("db", {}).get("seed_employee") or {}     # 读取配置中的演示员工，不写死在代码里
        if emp and not conn.execute(                           # 员工表为空时才写种子，避免覆盖真实数据
            "SELECT 1 FROM employees WHERE emp_id = ?", (emp["emp_id"],)  # 按工号查重
        ).fetchone():                                          # 取一行判断是否存在
            conn.execute(                                      # 写入脱敏员工主档
                "INSERT INTO employees(emp_id,name_mask,dept,grade,created_at) VALUES(?,?,?,?,?)",  # 占位符防注入
                (emp["emp_id"], emp["name_mask"], emp["dept"], emp["grade"], now_utc()),  # 参数化绑定
            )                                                  # 插入结束
            conn.execute(                                      # 写入当前生效薪酬基线
                "INSERT INTO salary_records(emp_id,base_salary_cents,currency,version_no,updated_at) VALUES(?,?,?,?,?)",  # 占位符
                (emp["emp_id"], int(emp["base_salary_cents"]), emp.get("currency", "CNY"), 1, now_utc()),  # 版本号从 1 开始
            )                                                  # 插入结束
            seeded = True                                      # 标记本次确实写入了种子
        for doc in cfg.get("kb", {}).get("seed_documents", []):  # 逐个写入配置里的制度文档
            if conn.execute(                                   # 按标题查重，保证重复启动不产生重复知识
                "SELECT 1 FROM kb_docs WHERE title = ?", (doc["title"],)  # 参数化查询
            ).fetchone():                                      # 已存在则跳过
                continue                                       # 继续下一个文档
            doc_id = new_id("doc")                             # 生成文档 ID
            conn.execute(                                      # 写入知识库原文表
                "INSERT INTO kb_docs(doc_id,title,content,source,created_at) VALUES(?,?,?,?,?)",  # 占位符
                (doc_id, doc["title"], doc["content"], doc["source"], now_utc()),  # 参数化绑定
            )                                                  # 插入结束
            if fts:                                            # 全文索引可用时同步写入索引表
                conn.execute(                                  # FTS5 虚表需手工同步，无外部内容表触发器
                    "INSERT INTO kb_fts(doc_id,title,content) VALUES(?,?,?)",  # 占位符
                    (doc_id, doc["title"], doc["content"]),   # 与原文表保持一致
                )                                              # 插入结束
    return {"db_path": db_path, "fts": fts, "seeded": seeded} # 返回初始化结果


def dumps(value: Any) -> str:
    """把任意结构序列化成紧凑 JSON 字符串。

    入参:
        value: 字典、列表等可序列化对象。
    出参:
        UTF-8 中文不转义的 JSON 字符串。
    失败时:
        含不可序列化对象时抛 TypeError，调用方需保证数据来源干净。
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))  # 紧凑格式，便于计算稳定哈希


def loads(value: str | None, default: Any = None) -> Any:
    """把数据库中的 JSON 字符串还原成对象。

    入参:
        value: 可能为 None 的 JSON 字符串。
        default: value 为空时返回的默认值。
    出参:
        还原后的对象或 default。
    失败时:
        JSON 损坏时抛 json.JSONDecodeError，属数据完整性问题，不静默吞掉。
    """
    return default if value is None else json.loads(value)     # 空值直接给默认值，避免下游判空遗漏
