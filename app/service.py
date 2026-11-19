"""业务服务层：版本状态机、双人复核硬约束、人工确认、生效事务与统一视图。"""

import sqlite3                                                  # 捕获数据库约束异常并转成可读错误码
from types import SimpleNamespace                               # 构造与创建请求同形的对象，复用 create_change 逻辑
from typing import Any                                          # 标注任意载荷

from app import kb                                             # 知识库入库与检索
from app.audit import append_audit, list_audit, verify_chain   # 审计写入、读取与完整性校验
from app.config import resolve_path                         # 路径解析
from app.db import (STATUS_APPROVED, STATUS_DRAFT,            # 状态常量
                    STATUS_EFFECTIVE, STATUS_PENDING_REVIEW,
                    STATUS_REJECTED, STATUS_RESEARCH_DONE,
                    STATUS_RESEARCHING, STATUS_REVIEW_ONE,
                    dumps, loads, now_utc, new_id, session)
from app.harness import run_research                          # 现场调研 harness
from app.response import BizError                              # 全部可预期失败都抛业务异常
from app.security import has_role, issue_token, mask_salary   # 角色判断、令牌签发、薪酬脱敏

_ALL_STATUSES = (                                              # 合法状态白名单，用于筛选参数校验
    STATUS_DRAFT, STATUS_RESEARCHING, STATUS_RESEARCH_DONE,    # 调研前的三种状态
    STATUS_PENDING_REVIEW, STATUS_REVIEW_ONE, STATUS_APPROVED, # 复核中的三种状态
    STATUS_EFFECTIVE, STATUS_REJECTED,                         # 两个终态
)                                                              # 白名单结束


def _get_version(conn: sqlite3.Connection, version_id: str) -> dict:
    """按 ID 读取变更版本。

    入参:
        conn: 数据库连接。
        version_id: 版本 ID。
    出参:
        版本字典（含明文 after_json，仅服务层内部使用）。
    失败时:
        不存在时抛 BizError("CHANGE_NOT_FOUND", 404)。
    """
    row = conn.execute(                                        # 按主键查询
        "SELECT * FROM salary_change_versions WHERE version_id=?",  # 占位符防注入
        (version_id,),                                         # 绑定 ID
    ).fetchone()                                               # 取一行
    if row is None:                                            # 未命中
        raise BizError("CHANGE_NOT_FOUND", "变更版本不存在", 404)  # 统一错误码
    return dict(row)                                           # 转成普通字典


def _assert_status(version: dict, allowed: tuple[str, ...]) -> None:
    """状态机守卫：不在允许状态就拒绝流转。

    入参:
        version: 版本字典。
        allowed: 允许执行当前操作的状态元组。
    出参:
        无返回值，通过时静默返回。
    失败时:
        状态不匹配抛 BizError("INVALID_STATUS", 409)，不做任何写入。
    """
    if version["status"] not in allowed:                      # 状态不符
        # 为什么卡在这里：状态机是防绕过的主线。若允许从 DRAFT 直接生效或重复复核，
        # 双签记录就算齐全也不代表流程合法；所有写操作前必须先过这一关。
        raise BizError("INVALID_STATUS",                     # 统一冲突错误码
                       f"当前状态 {version['status']} 不允许该操作", 409)  # 提示当前状态


def _view_version(conn: sqlite3.Connection, cfg: dict, actor: dict, row: dict) -> dict:
    """构造对外可见的版本视图，按角色脱敏。

    入参:
        conn: 数据库连接。
        cfg: 全局配置。
        actor: 当前身份。
        row: 版本原始行。
    出参:
        版本视图字典，含 before/after 的脱敏金额、复核轨迹与调研摘要。
    失败时:
        JSON 快照损坏时由 loads 抛 JSONDecodeError，走 500，属数据完整性事故。
    """
    is_admin = has_role(actor, "ADMIN")                       # 只有管理员可看明文金额
    enabled = bool(cfg["data_redline"]["mask_salary_for_non_admin"])  # 读取红线开关

    def _project(snapshot: dict) -> dict:
        """把薪酬快照投影成脱敏展示结构。

        入参:
            snapshot: 含 base_salary_cents 的快照字典。
        出参:
            {"base_salary_display", "base_salary_cents"(仅管理员), "currency", "version_no", "grade"}。
        失败时:
            缺少 base_salary_cents 时按 0 处理，不抛异常，避免历史脏数据让整个查询失败。
        """
        cents = int(snapshot.get("base_salary_cents", 0))    # 取分数值
        masked = mask_salary(cents, is_admin, enabled)       # 按角色决定掩码或明文
        out = {"base_salary_display": masked["base_salary_display"],  # 展示值
               "currency": snapshot.get("currency", "CNY"),  # 币种
               "version_no": snapshot.get("version_no"),     # 薪酬版本号
               "grade": snapshot.get("grade", "")}           # 职级
        if not masked["masked"]:                             # 管理员才附明文整数
            out["base_salary_cents"] = masked["base_salary_cents"]  # 供对账使用
        return out                                           # 返回投影结果

    reviews = [dict(r) for r in conn.execute(                # 查该版本的全部复核记录
        "SELECT review_id,reviewer_id,slot,decision,comment,created_at FROM review_records "
        "WHERE version_id=? ORDER BY slot ASC",              # 按复核位排序，便于看出第一/第二人
        (row["version_id"],),                               # 绑定版本 ID
    ).fetchall()]                                           # 转成字典列表
    session = conn.execute(                                  # 查关联调研会话摘要
        "SELECT session_id,status,confirmed_by,confirmed_at FROM research_sessions "
        "WHERE version_id=? ORDER BY created_at DESC LIMIT 1",  # 取最近一场
        (row["version_id"],),                               # 绑定版本 ID
    ).fetchone()                                            # 可能为空
    return {"version_id": row["version_id"],               # 版本 ID
            "emp_id": row["emp_id"],                       # 工号
            "status": row["status"],                       # 当前状态
            "reason": row["reason"],                       # 变更事由
            "operator_id": row["operator_id"],             # 经办人，供复核时对照
            "before": _project(loads(row["before_json"], {})),  # 变更前（脱敏）
            "after": _project(loads(row["after_json"], {})),    # 变更后（脱敏）
            "diff": _diff_view(row, is_admin, enabled),    # 差异对比
            "reviews": reviews,                            # 双人复核轨迹
            "research": dict(session) if session else None, # 调研会话摘要
            "created_at": row["created_at"],               # 创建时间
            "submitted_at": row["submitted_at"],           # 提交时间
            "effective_at": row["effective_at"]}           # 生效时间


def _diff_view(row: dict, is_admin: bool, enabled: bool) -> dict:
    """计算变更前后差额，同样按角色脱敏。

    入参:
        row: 版本原始行。
        is_admin: 是否管理员。
        enabled: 脱敏开关。
    出参:
        {"delta_display": "****" 或 "+¥2,000.00", "direction": "UP"/"DOWN"/"FLAT"/"MASKED"}。
    失败时:
        快照 JSON 损坏时抛 JSONDecodeError，由上层兜底。
    """
    before = int(loads(row["before_json"], {}).get("base_salary_cents", 0))  # 变更前分值
    after = int(loads(row["after_json"], {}).get("base_salary_cents", 0))    # 变更后分值
    if enabled and not is_admin:                           # 非管理员连差额也不给
        # 为什么卡在这里：即使金额被掩码，差额也能反推出调薪幅度，属于同一级别的敏感信息。
        return {"delta_display": "****", "direction": "MASKED"}  # 完全隐藏
    delta = after - before                                 # 计算差额（分）
    sign = "+" if delta > 0 else ""                       # 正数带加号
    return {"delta_display": f"{sign}¥{delta / 100:,.2f}",  # 转元展示
            "direction": "UP" if delta > 0 else ("DOWN" if delta < 0 else "FLAT")}  # 方向


def health(cfg: dict) -> dict:
    """健康检查数据，不泄露任何密钥或绝对路径。

    入参:
        cfg: 全局配置。
    出参:
        {"app","llm_mode","llm_model","db_file","prompts_loaded","audit"}。
    失败时:
        提示词文件缺失时抛 BizError/FILE 相关异常，让启动或探活立即报警。
    """
    from app.prompts import load_prompts                    # 延迟导入，避免与配置加载形成循环
    prompts = load_prompts(cfg["prompts"]["file"])         # 校验提示词可读
    with session(str(resolve_path(cfg["db"]["path"]))) as conn:  # 只读连接，退出即关闭
        audit = verify_chain(conn)                         # 顺带校验审计链
    return {"app": cfg["app"]["name"],                    # 应用名
            "llm_mode": cfg["llm"]["mode"],               # 模型调用模式
            "llm_model": cfg["llm"]["model"],             # 模型名（非密钥）
            "db_file": cfg["db"]["path"].split("/")[-1],  # 仅文件名，不暴露绝对路径
            "prompts_loaded": sorted(prompts.keys()),     # 已加载的提示词名
            "audit": audit}                               # 审计链状态


def sign_token(cfg: dict, payload: Any) -> dict:
    """签发令牌（服务层包装）。

    入参:
        cfg: 全局配置。
        payload: TokenIn 请求对象，含 actor_id 与 pin。
    出参:
        {"token","expires_at","actor"}。
    失败时:
        账号或口令错误抛 BizError("UNAUTHORIZED", 401)。
    """
    return issue_token(payload.actor_id, payload.pin, cfg) # 委托给安全模块，避免重复实现


def add_kb_document(conn: sqlite3.Connection, cfg: dict, actor: dict, payload: Any) -> dict:
    """知识库入库并记录审计。

    入参:
        conn: 写事务中的连接。
        cfg: 全局配置。
        actor: 当前身份（路由已限定 ADMIN）。
        payload: KBAddIn 请求对象。
    出参:
        {"doc_id","title","fts"}。
    失败时:
        写入失败抛 sqlite3.Error，由事务回滚并转成 500。
    """
    result = kb.add_document(conn, payload.title, payload.content, payload.source)  # 写原文与索引
    append_audit(conn, actor["actor_id"], "KB_ADD", result["doc_id"],  # 记录谁在什么时候加了制度文档
                 {"title": payload.title, "source": payload.source})  # 只写标题与来源，不写全文
    return result                                          # 返回入库结果


def search_kb(conn: sqlite3.Connection, cfg: dict, actor: dict, q: str, top_k: int = 3) -> dict:
    """知识库检索（只读，不写审计，避免高频查询污染链）。

    入参:
        conn: 数据库连接。
        cfg: 全局配置。
        actor: 当前身份。
        q: 检索词。
        top_k: 返回条数。
    出参:
        {"query": q, "hits": [...]}。
    失败时:
        检索失败由 kb 层抛 BizError("KB_SEARCH_FAILED")。
    """
    return {"query": q, "hits": kb.search(conn, q, top_k)}  # 委托知识库模块


def create_change(conn: sqlite3.Connection, cfg: dict, actor: dict, payload: Any) -> dict:
    """创建一个薪酬变更版本（DRAFT）。

    入参:
        conn: 写事务中的连接。
        cfg: 全局配置。
        actor: 当前身份（路由已限定 OPERATOR/ADMIN）。
        payload: ChangeCreateIn 请求对象。
    出参:
        版本视图（金额已按角色脱敏）。
    失败时:
        员工不存在抛 BizError("EMP_NOT_FOUND", 404)；数据库写入失败由事务回滚。
    """
    emp = conn.execute(                                        # 查员工主档
        "SELECT emp_id,name_mask,dept,grade FROM employees WHERE emp_id=?",  # 只取脱敏字段
        (payload.emp_id,),                                    # 绑定工号
    ).fetchone()                                               # 取一行
    if emp is None:                                            # 员工不存在
        raise BizError("EMP_NOT_FOUND", "员工不存在", 404)   # 统一错误码
    current = conn.execute(                                    # 查当前生效薪酬
        "SELECT base_salary_cents,currency,version_no FROM salary_records WHERE emp_id=?",  # 取基线
        (payload.emp_id,),                                    # 绑定工号
    ).fetchone()                                               # 取一行
    if current is None:                                       # 无薪酬基线不能凭空变更
        raise BizError("SALARY_BASE_MISSING", "缺少当前薪酬基线", 409)  # 拒绝
    before = {"base_salary_cents": int(current["base_salary_cents"]),  # 变更前快照
              "currency": current["currency"],               # 币种
              "version_no": int(current["version_no"]),     # 版本号，用于生效时乐观锁
              "grade": emp["grade"]}                        # 职级
    after = {"base_salary_cents": int(payload.new_base_salary_cents),  # 变更后内容
             "currency": current["currency"],               # 币种不变
             "version_no": int(current["version_no"]) + 1, # 预排版本号
             "grade": emp["grade"]}                        # 职级不变
    version_id = new_id("ver")                             # 生成版本 ID
    conn.execute(                                           # 写入版本表，原薪酬记录此刻绝不动
        "INSERT INTO salary_change_versions(version_id,emp_id,from_version_no,before_json,after_json,"
        "reason,research_question,operator_id,status,research_session_id,created_at,submitted_at,effective_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",                # 占位符防注入
        (version_id, payload.emp_id, before["version_no"], dumps(before), dumps(after),  # 快照与内容
         payload.reason, payload.research_question, actor["actor_id"], STATUS_DRAFT,  # 固化调研问题
         None, now_utc(), None, None),                     # 初始状态草稿
    )                                                       # 插入结束
    append_audit(conn, actor["actor_id"], "CHANGE_CREATE", version_id,  # 审计：只写哈希不写金额
                 {"emp_id": payload.emp_id, "reason": payload.reason,
                  "base_salary_cents": payload.new_base_salary_cents})  # 该键会被清洗成 ***
    return _view_version(conn, cfg, actor, _get_version(conn, version_id))  # 返回脱敏视图


def list_changes(conn: sqlite3.Connection, cfg: dict, actor: dict,
                 status: str | None = None, emp_id: str | None = None,
                 keyword: str | None = None) -> list[dict]:
    """列出变更版本，支持按状态、工号、关键词筛选（按创建时间倒序）。

    入参:
        conn: 数据库连接。
        cfg: 全局配置。
        actor: 当前身份，决定金额是否脱敏。
        status: 可选状态筛选，必须是合法状态值，None 表示不筛选。
        emp_id: 可选工号筛选，None 表示不筛选。
        keyword: 可选关键词，匹配事由 / 版本 ID / 经办人，None 表示不筛选。
    出参:
        版本视图列表。
    失败时:
        状态值非法抛 BizError("INVALID_STATUS_FILTER", 422)；数据库读取失败抛 sqlite3.Error，由全局处理器转成 500。
    """
    sql = ["SELECT * FROM salary_change_versions WHERE 1=1"]    # 用 1=1 作为拼接起点，后续条件统一以 AND 追加
    args: list[Any] = []                                        # 与占位符一一对应的参数列表，杜绝字符串拼 SQL
    if status:                                                  # 传了状态才加条件
        if status not in _ALL_STATUSES:                         # 状态值必须来自白名单
            raise BizError("INVALID_STATUS_FILTER", "未知的状态筛选值", 422)  # 拼错状态不应静默返回空列表
        sql.append("AND status=?")                              # 追加状态条件
        args.append(status)                                     # 绑定状态值
    if emp_id:                                                  # 传了工号才加条件
        sql.append("AND emp_id=?")                              # 精确匹配工号
        args.append(emp_id)                                     # 绑定工号
    if keyword:                                                 # 传了关键词才加模糊条件
        like = f"%{keyword.strip()}%"                           # 两侧加通配符，作为参数传入而非拼进 SQL
        sql.append("AND (reason LIKE ? OR version_id LIKE ? OR operator_id LIKE ?)")  # 三个可检索字段
        args.extend([like, like, like])                         # 同一关键词绑定三次
    sql.append("ORDER BY created_at DESC")                      # 倒序，最近创建的在前
    rows = conn.execute(" ".join(sql), args).fetchall()         # 参数化执行，避免注入
    return [_view_version(conn, cfg, actor, dict(r)) for r in rows]  # 逐个构造脱敏视图


def get_chain(conn: sqlite3.Connection, cfg: dict, actor: dict, version_id: str) -> dict:
    """读取同一员工的历史版本链，用于判断本次调薪是否重复或倒退。

    入参:
        conn: 数据库连接。
        cfg: 全局配置。
        actor: 当前身份，决定金额是否脱敏。
        version_id: 链中任意一个版本 ID，用它的工号定位整条链。
    出参:
        {"emp_id", "current": 当前生效薪酬摘要或 None, "items": 版本摘要列表（按创建时间升序）}。
    失败时:
        版本不存在抛 BizError("CHANGE_NOT_FOUND", 404)；JSON 损坏抛 JSONDecodeError 走 500。
    """
    base = _get_version(conn, version_id)                       # 先取锚点版本，拿工号
    is_admin = has_role(actor, "ADMIN")                         # 只有管理员看明文金额
    enabled = bool(cfg["data_redline"]["mask_salary_for_non_admin"])  # 脱敏开关
    record = conn.execute(                                      # 查该员工当前生效的薪酬基线
        "SELECT base_salary_cents,currency,version_no,updated_at FROM salary_records WHERE emp_id=?",  # 只取必要列
        (base["emp_id"],),                                      # 绑定工号
    ).fetchone()                                                # 取一行，可能为空
    current = None                                              # 默认没有基线
    if record is not None:                                      # 有基线才构造摘要
        masked = mask_salary(int(record["base_salary_cents"]), is_admin, enabled)  # 按角色脱敏
        current = {"version_no": int(record["version_no"]),     # 当前薪酬版本号
                   "base_salary_display": masked["base_salary_display"],  # 展示金额
                   "updated_at": record["updated_at"]}          # 基线最后更新时间
    rows = conn.execute(                                        # 查同工号的全部变更版本
        "SELECT * FROM salary_change_versions WHERE emp_id=? ORDER BY created_at ASC",  # 升序便于看演进
        (base["emp_id"],),                                      # 绑定工号
    ).fetchall()                                                # 取回列表
    items = []                                                  # 收集摘要
    for r in rows:                                              # 逐条构造轻量摘要，不重复展开复核明细
        row = dict(r)                                           # 转成字典便于读取
        diff = _diff_view(row, is_admin, enabled)               # 差额同样脱敏
        items.append({"version_id": row["version_id"],           # 版本 ID
                      "status": row["status"],                  # 状态
                      "operator_id": row["operator_id"],        # 经办人
                      "reason": row["reason"],                  # 事由
                      "after_display": mask_salary(int(loads(row["after_json"], {}).get("base_salary_cents", 0)),  # 变更后金额
                                                   is_admin, enabled)["base_salary_display"],  # 按角色脱敏
                      "delta_display": diff["delta_display"],   # 差额展示值
                      "created_at": row["created_at"],          # 创建时间
                      "effective_at": row["effective_at"],      # 生效时间
                      "is_current": row["version_id"] == version_id})  # 标记锚点版本，前端高亮
    return {"emp_id": base["emp_id"], "current": current, "items": items}  # 返回整条链


def clone_change(conn: sqlite3.Connection, cfg: dict, actor: dict,
                 version_id: str, payload: Any) -> dict:
    """基于一个被驳回的版本重建草稿，避免重复手工录入。

    入参:
        conn: 写事务中的连接。
        cfg: 全局配置。
        actor: 当前身份（路由已限定 OPERATOR/ADMIN）。
        version_id: 源版本 ID，必须是 REJECTED。
        payload: ChangeCloneIn 请求对象，金额 / 事由 / 问题均可留空。
    出参:
        新建草稿的脱敏视图，含 source_version_id。
    失败时:
        源版本不存在抛 CHANGE_NOT_FOUND(404)；状态不是 REJECTED 抛 INVALID_STATUS(409)；
        写入失败由事务回滚，源版本不受影响。
    """
    src = _get_version(conn, version_id)                        # 读取源版本
    if src["status"] != STATUS_REJECTED:                        # 只有驳回态需要重提
        # 为什么卡在这里：允许从任意状态复制会产生"平行版本"，同一员工同时存在两条待复核记录，
        # 生效时后写的那条会覆盖前一条，审计上无法判断哪次调薪是真实意图。
        raise BizError("INVALID_STATUS", "只有被驳回的版本可以复制重建", 409)  # 明确拒绝
    src_after = loads(src["after_json"], {})                    # 取源版本的变更后快照
    cents = payload.new_base_salary_cents                       # 优先用调用方重新确认的金额
    if not cents:                                               # 未填则沿用原金额
        cents = int(src_after.get("base_salary_cents", 0))      # 从快照取原金额
    if cents <= 0:                                              # 原金额缺失属于脏数据
        raise BizError("SALARY_BASE_MISSING", "源版本缺少有效金额，请直接新建版本", 409)  # 拒绝，避免生成 0 元版本
    reason = (payload.reason or f"{src['reason']}（基于被驳回版本重建）").strip()  # 默认在原事由上标注来源
    question = payload.research_question or _get_research_question(conn, version_id)  # 沿用原调研问题
    if not question:                                            # 原问题缺失时给一个可执行的默认值
        question = "核实岗位职级、绩效依据与生效月份"            # 兜底问题，保证能发起调研
    created = create_change(conn, cfg, actor, SimpleNamespace(   # 复用创建逻辑，保证 before 取的是最新基线
        emp_id=src["emp_id"],                                   # 同工号
        new_base_salary_cents=int(cents),                       # 重新确认的金额
        reason=reason,                                          # 新事由
        research_question=question))                            # 调研问题
    append_audit(conn, actor["actor_id"], "CHANGE_CLONE", created["version_id"],  # 审计：记录重建来源
                 {"source_version_id": version_id, "emp_id": src["emp_id"]})  # 只写 ID，不写金额
    created["source_version_id"] = version_id                   # 回传来源，前端可显示"由 xx 重建"
    return created                                              # 返回新草稿视图


def get_change(conn: sqlite3.Connection, cfg: dict, actor: dict, version_id: str) -> dict:
    """读取单个变更版本详情。

    入参:
        conn: 数据库连接。
        cfg: 全局配置。
        actor: 当前身份。
        version_id: 版本 ID。
    出参:
        版本视图。
    失败时:
        版本不存在抛 BizError("CHANGE_NOT_FOUND", 404)。
    """
    return _view_version(conn, cfg, actor, _get_version(conn, version_id))  # 取行并脱敏


def run_change_research(conn: sqlite3.Connection, cfg: dict, actor: dict,
                        version_id: str, payload: Any) -> dict:
    """为版本执行一次现场调研，生成待人工确认的结论。

    入参:
        conn: 写事务中的连接。
        cfg: 全局配置。
        actor: 当前身份。
        version_id: 版本 ID。
        payload: ResearchRunIn 请求对象。
    出参:
        {"session_id","status","rounds","conclusion","version_status"}，版本状态回到 DRAFT。
    失败时:
        状态不符抛 INVALID_STATUS；模型/结论异常抛 RESEARCH_*；事务回滚后版本仍是 DRAFT。
    """
    version = _get_version(conn, version_id)                  # 读取版本
    _assert_status(version, (STATUS_DRAFT,))                 # 只有草稿能发起调研
    conn.execute(                                           # 先标记调研中，便于运维观测
        "UPDATE salary_change_versions SET status=? WHERE version_id=?",  # 占位符
        (STATUS_RESEARCHING, version_id),                  # 短暂状态
    )                                                       # 更新结束
    question = payload.question or version.get("research_question") or ""  # 优先用本次覆盖问题，其次用版本固化的问题
    if not question:                                        # 两者都为空时用事由兜底
        question = _get_research_question(conn, version_id) # 生成默认问题，保证非空
    version_ctx = dict(version)                             # 复制版本信息
    version_ctx["reason"] = version["reason"]              # 事由传给 harness（会被限长）
    version_ctx["grade"] = ""                              # 职级不单独查，避免把员工信息外发
    result = run_research(conn, cfg, version_ctx, question,  # 执行 harness 多轮调研
                          dict(payload.field_answers), actor["actor_id"])  # 传现场答复
    conn.execute(                                           # 调研结束，状态回到草稿，等待人工确认
        "UPDATE salary_change_versions SET status=?,research_session_id=? WHERE version_id=?",  # 占位符
        (STATUS_DRAFT, result["session_id"], version_id),  # 挂上会话 ID 但不改变流程状态
    )                                                       # 更新结束
    append_audit(conn, actor["actor_id"], "RESEARCH_RUN", version_id,  # 审计调研动作
                 {"session_id": result["session_id"], "rounds": result["rounds"]})  # 只写轮数
    return {"session_id": result["session_id"], "status": result["status"],  # 返回会话摘要
            "rounds": result["rounds"], "conclusion": result["conclusion"],
            "transcript": result["transcript"],            # 完整工具调用轨迹，供追溯与回归断言
            "version_status": STATUS_DRAFT}                 # 明确仍未进入复核


def _get_research_question(conn: sqlite3.Connection, version_id: str) -> str:
    """取得调研问题：优先用最近会话里的问题，否则用事由生成默认问题。

    入参:
        conn: 数据库连接。
        version_id: 版本 ID。
    出参:
        非空的问题字符串。
    失败时:
        版本不存在时由 _get_version 抛 CHANGE_NOT_FOUND。
    """
    row = conn.execute(                                        # 查最近一场会话
        "SELECT question FROM research_sessions WHERE version_id=? ORDER BY created_at DESC LIMIT 1",  # 倒序取一
        (version_id,),                                        # 绑定版本 ID
    ).fetchone()                                               # 可能为空
    if row and row["question"]:                             # 有历史问题则复用
        return str(row["question"])                         # 返回问题
    ver = _get_version(conn, version_id)                    # 取版本以拿到事由
    return f"请围绕以下变更事由完成现场取证：{ver['reason']}"  # 默认问题，保证非空


def confirm_research(conn: sqlite3.Connection, cfg: dict, actor: dict,
                     version_id: str, payload: Any) -> dict:
    """人工确认调研结论，确认后版本才能提交复核。

    入参:
        conn: 写事务中的连接。
        cfg: 全局配置。
        actor: 当前身份。
        version_id: 版本 ID。
        payload: ResearchConfirmIn 请求对象。
    出参:
        {"version_id","status","session_id","confirmed_by"}。
    失败时:
        confirm 不为 True、未开启强制确认、会话不存在或状态不对时抛对应 BizError，事务回滚。
    """
    # 为什么卡在这里：这是"人在回路"的唯一闸口。模型结论再完整也只是建议，
    # 必须由具名人员显式确认并留痕，否则出现错误结论时无法界定责任，也无法证明有人审过。
    if not bool(cfg["harness"]["require_human_confirm"]):   # 配置被改成 false 属于危险配置
        raise BizError("HUMAN_CONFIRM_DISABLED", "配置禁止关闭人工确认", 409)  # 直接拒绝
    if not payload.confirm:                                # 客户端没有显式确认
        raise BizError("HUMAN_CONFIRM_REQUIRED", "必须显式确认调研结论")  # 防止误触
    version = _get_version(conn, version_id)               # 读取版本
    _assert_status(version, (STATUS_DRAFT,))               # 只有草稿阶段能确认
    row = conn.execute(                                    # 取该版本最近的待确认会话
        "SELECT session_id,status FROM research_sessions WHERE version_id=? "
        "ORDER BY created_at DESC LIMIT 1",                # 倒序取一
        (version_id,),                                     # 绑定版本 ID
    ).fetchone()                                           # 可能为空
    if row is None or row["status"] != "WAIT_CONFIRM":     # 没有待确认结论
        raise BizError("RESEARCH_NOT_READY", "没有待人工确认的调研结论", 409)  # 拒绝
    conn.execute(                                          # 标记会话已确认
        "UPDATE research_sessions SET status='CONFIRMED',confirmed_by=?,confirmed_at=? WHERE session_id=?",  # 占位符
        (actor["actor_id"], now_utc(), row["session_id"]), # 绑定确认人与时间
    )                                                      # 更新结束
    conn.execute(                                          # 推进版本状态
        "UPDATE salary_change_versions SET status=?,research_session_id=? WHERE version_id=?",  # 占位符
        (STATUS_RESEARCH_DONE, row["session_id"], version_id),  # 结论已归档
    )                                                      # 更新结束
    append_audit(conn, actor["actor_id"], "RESEARCH_CONFIRM", version_id,  # 审计人工确认人
                 {"session_id": row["session_id"], "note": payload.note})  # 备注不含金额
    return {"version_id": version_id, "status": STATUS_RESEARCH_DONE,  # 返回结果
            "session_id": row["session_id"], "confirmed_by": actor["actor_id"]}  # 具名确认人


def submit_change(conn: sqlite3.Connection, cfg: dict, actor: dict, version_id: str) -> dict:
    """提交版本进入双人复核。

    入参:
        conn: 写事务中的连接。
        cfg: 全局配置。
        actor: 当前身份。
        version_id: 版本 ID。
    出参:
        {"version_id","status","submitted_at"}。
    失败时:
        未确认调研结论或状态不符抛 BizError(..., 409)，不写复核记录。
    """
    version = _get_version(conn, version_id)               # 读取版本
    _assert_status(version, (STATUS_RESEARCH_DONE,))       # 必须已完成调研确认
    row = conn.execute(                                    # 复核前再查一次会话确认状态
        "SELECT status FROM research_sessions WHERE session_id=?",  # 按挂接的会话 ID 查
        (version["research_session_id"],),                # 绑定会话 ID
    ).fetchone()                                           # 可能为空
    # 为什么卡在这里：状态字段可能被直接改库绕过，因此提交时再核对会话本身；
    # 没有"已由人确认"的调研结论，复核人就没有判断依据，等于让双签变成走过场。
    if row is None or row["status"] != "CONFIRMED":       # 结论未确认
        raise BizError("RESEARCH_NOT_CONFIRMED", "调研结论未经人工确认，不能提交复核", 409)  # 硬拒绝
    ts = now_utc()                                         # 生成提交时间
    conn.execute(                                          # 推进到待第一人复核
        "UPDATE salary_change_versions SET status=?,submitted_at=? WHERE version_id=?",  # 占位符
        (STATUS_PENDING_REVIEW, ts, version_id),          # 绑定状态与时间
    )                                                      # 更新结束
    append_audit(conn, actor["actor_id"], "CHANGE_SUBMIT", version_id, {})  # 审计提交动作
    return {"version_id": version_id, "status": STATUS_PENDING_REVIEW, "submitted_at": ts}  # 返回结果


def review_change(conn: sqlite3.Connection, cfg: dict, actor: dict,
                  version_id: str, payload: Any) -> dict:
    """执行一次复核，落实双人、不同人、一人一次三条硬约束。

    入参:
        conn: 写事务中的连接（调用方必须用 BEGIN IMMEDIATE 抢锁）。
        cfg: 全局配置。
        actor: 当前身份（路由已限定 REVIEWER/ADMIN）。
        version_id: 版本 ID。
        payload: ReviewIn 请求对象。
    出参:
        {"version_id","status","slot","decision","reviewer_id"}。
    失败时:
        状态不符、本人复核、重复复核、违反数据库唯一约束或触发器时抛对应 BizError，事务回滚。
    """
    version = _get_version(conn, version_id)               # 事务内重新读状态，避免读到锁前的旧值
    _assert_status(version, (STATUS_PENDING_REVIEW, STATUS_REVIEW_ONE))  # 只有待复核/已一签可复核
    reviewer_id = actor["actor_id"]                       # 取复核人 ID
    # 为什么卡在这里：数据库触发器是最后一道防线，但触发器报错不可读且无法区分场景。
    # 服务层先做同样的判断，给出明确错误码，同时形成"服务层 + 数据库层"双重校验，改库也绕不过。
    if reviewer_id == version["operator_id"]:             # 经办人复核自己的变更
        raise BizError("REVIEW_SAME_PERSON", "复核人不能与经办人相同", 409)  # 硬拒绝
    exists = conn.execute(                                 # 查该复核人是否已复核过此版本
        "SELECT 1 FROM review_records WHERE version_id=? AND reviewer_id=?",  # 参数化
        (version_id, reviewer_id),                        # 绑定版本与人
    ).fetchone()                                           # 取一行
    if exists:                                            # 同一人想签第二笔
        raise BizError("REVIEW_DUPLICATE", "同一复核人不能复核两次", 409)  # 硬拒绝
    slot = 1 if version["status"] == STATUS_PENDING_REVIEW else 2  # 按状态分配复核位
    try:                                                   # 保护数据库约束
        conn.execute(                                      # 写入复核轨迹
            "INSERT INTO review_records(review_id,version_id,reviewer_id,slot,decision,comment,created_at) "
            "VALUES(?,?,?,?,?,?,?)",                      # 占位符防注入
            (new_id("rev"), version_id, reviewer_id, slot, payload.decision,  # 绑定各字段
             payload.comment, now_utc()),               # 意见与时间
        )                                                  # 插入结束
    except sqlite3.IntegrityError as e:                    # 唯一约束或触发器 ABORT
        raise BizError("REVIEW_CONSTRAINT", f"复核约束拦截：{type(e).__name__}")  # 不含 SQL 细节
    if payload.decision == "REJECT":                      # 任一复核人驳回
        new_status = STATUS_REJECTED                      # 版本终止，不能继续流转
        action = "REVIEW_REJECT"                          # 审计动作名
    elif slot == 1:                                       # 第一人通过
        new_status = STATUS_REVIEW_ONE                    # 等第二人
        action = "REVIEW_APPROVE_ONE"                    # 审计动作名
    else:                                                 # 第二人通过
        new_status = STATUS_APPROVED                     # 双人齐备，待管理员生效
        action = "REVIEW_APPROVE_TWO"                    # 审计动作名
    conn.execute(                                          # 推进版本状态
        "UPDATE salary_change_versions SET status=? WHERE version_id=?",  # 占位符
        (new_status, version_id),                        # 绑定新状态
    )                                                      # 更新结束
    append_audit(conn, reviewer_id, action, version_id,  # 审计复核动作，复核意见不含金额
                 {"slot": slot, "decision": payload.decision, "comment": payload.comment})  # 载荷清洗后入库
    return {"version_id": version_id, "status": new_status,  # 返回结果
            "slot": slot, "decision": payload.decision, "reviewer_id": reviewer_id}  # 回到调用方


def effective_change(conn: sqlite3.Connection, cfg: dict, actor: dict,
                     version_id: str, payload: Any) -> dict:
    """双人通过后由管理员生效，原子更新薪酬记录。

    入参:
        conn: 写事务中的连接。
        cfg: 全局配置。
        actor: 当前身份（路由已限定 ADMIN）。
        version_id: 版本 ID。
        payload: EffectiveIn 请求对象。
    出参:
        生效后的版本视图。
    失败时:
        状态不符、双签不齐、复核人相同、乐观锁冲突时抛 BizError(..., 409)，事务整体回滚。
    """
    version = _get_version(conn, version_id)               # 读取版本
    _assert_status(version, (STATUS_APPROVED,))            # 必须已双人通过
    approvals = conn.execute(                             # 重新核对复核轨迹，不信任状态字段
        "SELECT reviewer_id,slot,decision FROM review_records WHERE version_id=? ORDER BY slot ASC",  # 按位排序
        (version_id,),                                    # 绑定版本 ID
    ).fetchall()                                           # 取回列表
    # 为什么卡在这里：状态字段可能被直接改库或旧代码写脏，生效是不可逆动作。
    # 必须重新验证"恰好两条 APPROVE、两个不同人、都不等于经办人"，否则单人就能把薪酬改掉。
    if len(approvals) != 2:                               # 数量不对
        raise BizError("DUAL_REVIEW_INCOMPLETE", "双人复核记录不齐，禁止生效", 409)  # 硬拒绝
    if any(r["decision"] != "APPROVE" for r in approvals):  # 存在驳回记录
        raise BizError("DUAL_REVIEW_INCOMPLETE", "存在驳回记录，禁止生效", 409)  # 硬拒绝
    ids = {r["reviewer_id"] for r in approvals}           # 复核人集合
    if len(ids) != 2 or version["operator_id"] in ids:    # 同一人或经办人混入
        raise BizError("DUAL_REVIEW_INCOMPLETE", "复核人不满足双人且非经办人约束", 409)  # 硬拒绝
    after = loads(version["after_json"], {})              # 取变更后内容
    ts = now_utc()                                        # 生效时间
    cur = conn.execute(                                   # 乐观锁更新当前薪酬
        "UPDATE salary_records SET base_salary_cents=?,version_no=?,updated_at=? "
        "WHERE emp_id=? AND version_no=?",                # version_no 条件防并发覆盖
        (int(after["base_salary_cents"]), int(after["version_no"]), ts,  # 绑定新值与时间
         version["emp_id"], int(version["from_version_no"])),  # 绑定工号与期望版本
    )                                                      # 执行结束
    if cur.rowcount != 1:                                 # 没更新到唯一一行
        raise BizError("VERSION_CONFLICT", "薪酬基线已被其他变更更新，请基于最新版本重提", 409)  # 拒绝
    conn.execute(                                          # 标记版本已生效
        "UPDATE salary_change_versions SET status=?,effective_at=? WHERE version_id=?",  # 占位符
        (STATUS_EFFECTIVE, ts, version_id),               # 绑定状态与时间
    )                                                      # 更新结束
    append_audit(conn, actor["actor_id"], "CHANGE_EFFECTIVE", version_id,  # 审计生效动作
                 {"emp_id": version["emp_id"], "note": payload.note,
                  "base_salary_cents": after["base_salary_cents"]})  # 金额键会被清洗成 ***
    return _view_version(conn, cfg, actor, _get_version(conn, version_id))  # 返回脱敏视图


def get_audit(conn: sqlite3.Connection, cfg: dict, actor: dict, limit: int = 100) -> dict:
    """管理员查看审计流水与链完整性。

    入参:
        conn: 数据库连接。
        cfg: 全局配置。
        actor: 当前身份（路由已限定 ADMIN）。
        limit: 返回条数。
    出参:
        {"chain": 校验结果, "items": 流水列表}。
    失败时:
        数据库读取失败抛 sqlite3.Error，由全局处理器转成 500。
    """
    safe_limit = max(1, min(int(limit), 500))             # 夹住上限，避免一次导出全链
    return {"chain": verify_chain(conn), "items": list_audit(conn, safe_limit)}  # 返回链与流水


def verify_audit(conn: sqlite3.Connection) -> dict:
    """单独校验审计链完整性。

    入参:
        conn: 数据库连接。
    出参:
        {"ok","count","broken_at"}。
    失败时:
        数据库读取失败抛 sqlite3.Error；链断裂返回 ok=False 而不抛异常。
    """
    return verify_chain(conn)                              # 委托审计模块
