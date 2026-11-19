"""FastAPI 入口：只负责路由、依赖注入、事务边界与统一响应封装。"""

from contextlib import asynccontextmanager                    # 定义应用启动/关闭生命周期

from fastapi import Depends, FastAPI, Query, Request          # 路由、依赖、查询参数与请求对象
from fastapi.responses import FileResponse, RedirectResponse  # 返回同源静态前端页面与根路径跳转

from app import db, service                                  # 数据库上下文与业务服务层
from app.config import load_config, resolve_path            # 加载 config.yaml 与解析前端文件路径
from app.llm import QwenClient                              # 启动时校验本地千问地址是否合法
from app.models import (ChangeCloneIn, ChangeCreateIn, EffectiveIn,  # 全部请求体模型
                        KBAddIn, ResearchConfirmIn,
                        ResearchRunIn, ReviewIn, TokenIn)
from app.prompts import load_prompts                        # 启动时校验提示词文件可读
from app.response import BizError, install_handlers, ok      # 统一响应与全局异常处理
from app.security import issue_demo_token, require_roles      # 角色校验依赖工厂与演示令牌签发


@asynccontextmanager                                          # 声明异步生命周期上下文
async def lifespan(app: FastAPI):
    """应用启动：加载配置、初始化数据库、校验提示词与模型地址。

    入参:
        app: FastAPI 应用实例。
    出参:
        迭代一次，运行期可通过 request.app.state 读取配置与数据库路径。
    失败时:
        任一初始化步骤失败都会抛异常并阻止服务启动，绝不"带病上线"。
    """
    cfg = load_config()                                       # 读取并校验全局配置
    init_info = db.init_db(cfg)                              # 建表、触发器、全文索引、种子数据
    load_prompts(cfg["prompts"]["file"])                     # 提示词缺失时立即失败，别等调研时才炸
    QwenClient(cfg)                                          # http 模式下地址不合法时立即失败
    app.state.cfg = cfg                                      # 挂到应用状态，路由只读不改写
    app.state.db_path = init_info["db_path"]                # 保存绝对数据库路径，避免每请求重复解析
    app.state.init_info = init_info                          # 保存初始化结果，供 /health 展示
    yield                                                    # 应用运行期
    # 关闭阶段无需释放资源：每个请求的连接都在 session 上下文里即时关闭。


app = FastAPI(title="薪酬变更版本与双人复核 · 现场调研智能体", lifespan=lifespan)  # 创建应用
install_handlers(app)                                        # 注册统一异常处理器，覆盖所有失败路径


def _db_path(request: Request) -> str:
    """从应用状态取数据库路径。

    入参:
        request: 当前请求。
    出参:
        数据库绝对路径字符串。
    失败时:
        lifespan 未执行时抛 AttributeError，属启动顺序错误，由全局处理器转成 500。
    """
    return request.app.state.db_path                         # 启动期已解析好，直接使用


@app.get("/health")                                           # 探活接口，无需鉴权
async def get_health(request: Request) -> dict:
    """健康检查。

    入参:
        request: 当前请求，用于读取应用状态。
    出参:
        统一结构，data 含应用名、模型模式、提示词清单与审计链状态。
    失败时:
        提示词或数据库异常时由全局处理器返回 ok=false 的统一结构。
    """
    return ok(service.health(request.app.state.cfg))        # 统一封装返回体


@app.post("/api/v1/auth/token")                              # 令牌签发，登录入口
async def post_token(request: Request, body: TokenIn) -> dict:
    """签发访问令牌。

    入参:
        request: 当前请求。
        body: 账号 ID 与口令。
    出参:
        统一结构，data 为 token/expires_at/actor。
    失败时:
        账号或口令错误抛 UNAUTHORIZED(401)，统一结构返回。
    """
    return ok(service.sign_token(request.app.state.cfg, body))  # 委托服务层校验口令


@app.get("/api/v1/auth/demo")                                # 前端"打开即看板"的演示令牌入口
async def get_demo_token(request: Request) -> dict:
    """签发免登录演示令牌。

    入参:
        request: 当前请求，用于读取应用状态中的配置。
    出参:
        统一结构，data 为 token/expires_at/actor。
    失败时:
        config.yaml 的 ui.auto_login_actor 为空或账号不存在时抛 DEMO_LOGIN_DISABLED(403)，
        前端捕获后退回手输口令的登录页，不会出现"半登录"状态。
    """
    return ok(issue_demo_token(request.app.state.cfg))       # 口令仍由服务端从环境变量注入


@app.post("/api/v1/kb/documents")                            # 知识库文档入库
async def post_kb_document(request: Request, body: KBAddIn,
                           actor: dict = Depends(require_roles("ADMIN"))) -> dict:
    """新增知识库文档。

    入参:
        request: 当前请求。
        body: 文档标题、正文与来源。
        actor: 已通过 ADMIN 角色校验的身份。
    出参:
        统一结构，data 为 doc_id/title/fts。
    失败时:
        无权限被依赖拦截为 FORBIDDEN(403)；写库失败事务回滚并返回 500。
    """
    with db.session(_db_path(request)) as conn:             # 打开请求级连接
        with db.transaction(conn):                         # 写操作统一在事务里
            data = service.add_kb_document(conn, request.app.state.cfg, actor, body)  # 执行入库与审计
    return ok(data)                                        # 提交后统一封装


@app.get("/api/v1/kb/search")                               # 知识库检索（只读）
async def get_kb_search(request: Request, q: str = Query(default="", max_length=200),
                        top_k: int = Query(default=3, ge=1, le=20),
                        actor: dict = Depends(require_roles("OPERATOR", "REVIEWER", "ADMIN"))) -> dict:
    """检索制度知识库。

    入参:
        request: 当前请求。
        q: 检索词，最长 200 字符。
        top_k: 返回条数，1~20。
        actor: 已校验的任一登录角色。
    出参:
        统一结构，data 为 query 与 hits。
    失败时:
        参数越界返回 VALIDATION_ERROR(422)；检索失败返回 KB_SEARCH_FAILED。
    """
    with db.session(_db_path(request)) as conn:             # 只读连接
        data = service.search_kb(conn, request.app.state.cfg, actor, q, top_k)  # 执行检索
    return ok(data)                                        # 统一封装


@app.post("/api/v1/changes")                                # 创建薪酬变更版本
async def post_change(request: Request, body: ChangeCreateIn,
                      actor: dict = Depends(require_roles("OPERATOR", "ADMIN"))) -> dict:
    """创建变更版本（DRAFT）。

    入参:
        request: 当前请求。
        body: 员工工号、新薪酬、事由、调研问题。
        actor: 已通过 OPERATOR/ADMIN 角色校验的身份。
    出参:
        统一结构，data 为脱敏后的版本视图。
    失败时:
        员工不存在返回 EMP_NOT_FOUND(404)；参数不合法返回 VALIDATION_ERROR(422)。
    """
    with db.session(_db_path(request)) as conn:             # 打开连接
        with db.transaction(conn):                         # 版本创建与审计必须同事务
            data = service.create_change(conn, request.app.state.cfg, actor, body)  # 执行创建
    return ok(data)                                        # 统一封装


@app.get("/api/v1/changes")                                 # 版本列表（支持筛选）
async def get_changes(request: Request,
                      status: str | None = Query(default=None, max_length=32),  # 状态筛选
                      emp_id: str | None = Query(default=None, max_length=64),  # 工号筛选
                      q: str | None = Query(default=None, max_length=100),      # 关键词筛选
                      actor: dict = Depends(require_roles("OPERATOR", "REVIEWER", "ADMIN"))) -> dict:
    """列出变更版本，可按状态 / 工号 / 关键词筛选。

    入参:
        request: 当前请求。
        status: 可选状态筛选值，非法值返回 422。
        emp_id: 可选工号筛选。
        q: 可选关键词，匹配事由 / 版本 ID / 经办人。
        actor: 已校验身份，决定金额是否脱敏。
    出参:
        统一结构，data 为版本视图列表（仍是数组，兼容既有前端）。
    失败时:
        状态值非法返回 INVALID_STATUS_FILTER(422)；数据库读取失败返回 INTERNAL_ERROR(500)。
    """
    with db.session(_db_path(request)) as conn:             # 只读连接
        data = service.list_changes(conn, request.app.state.cfg, actor,  # 查询并脱敏
                                    status=status, emp_id=emp_id, keyword=q)  # 透传筛选条件
    return ok(data)                                        # 统一封装


@app.get("/api/v1/changes/{version_id}")                    # 版本详情
async def get_change(request: Request, version_id: str,
                     actor: dict = Depends(require_roles("OPERATOR", "REVIEWER", "ADMIN"))) -> dict:
    """读取单个版本详情。

    入参:
        request: 当前请求。
        version_id: 路径中的版本 ID。
        actor: 已校验身份。
    出参:
        统一结构，data 为版本视图（含复核轨迹与差异对比）。
    失败时:
        版本不存在返回 CHANGE_NOT_FOUND(404)。
    """
    with db.session(_db_path(request)) as conn:             # 只读连接
        data = service.get_change(conn, request.app.state.cfg, actor, version_id)  # 查询并脱敏
    return ok(data)                                        # 统一封装


@app.get("/api/v1/changes/{version_id}/chain")              # 同一员工的历史版本链
async def get_change_chain(request: Request, version_id: str,
                           actor: dict = Depends(require_roles("OPERATOR", "REVIEWER", "ADMIN"))) -> dict:
    """读取某员工的历史版本链与当前薪酬基线。

    入参:
        request: 当前请求。
        version_id: 链中任意一个版本 ID。
        actor: 已校验身份，决定金额是否脱敏。
    出参:
        统一结构，data 为 {"emp_id","current","items"}。
    失败时:
        版本不存在返回 CHANGE_NOT_FOUND(404)。
    """
    with db.session(_db_path(request)) as conn:             # 只读连接
        data = service.get_chain(conn, request.app.state.cfg, actor, version_id)  # 查询整条链
    return ok(data)                                        # 统一封装


@app.post("/api/v1/changes/{version_id}/clone")             # 基于被驳回版本重建草稿
async def post_clone(request: Request, version_id: str, body: ChangeCloneIn,
                     actor: dict = Depends(require_roles("OPERATOR", "ADMIN"))) -> dict:
    """把被驳回的版本复制成一个新草稿。

    入参:
        request: 当前请求。
        version_id: 源版本 ID，必须处于 REJECTED。
        body: 可覆盖金额 / 事由 / 调研问题，全部留空则沿用原版本。
        actor: 已通过 OPERATOR/ADMIN 校验的身份。
    出参:
        统一结构，data 为新草稿视图，含 source_version_id。
    失败时:
        源版本不存在返回 404；状态不是 REJECTED 返回 INVALID_STATUS(409)；事务回滚，源版本不变。
    """
    with db.session(_db_path(request)) as conn:             # 打开连接
        with db.transaction(conn):                          # 新版本与审计同事务
            data = service.clone_change(conn, request.app.state.cfg, actor, version_id, body)  # 执行重建
    return ok(data)                                        # 统一封装


@app.post("/api/v1/changes/{version_id}/research/run")     # 启动现场调研
async def post_research_run(request: Request, version_id: str, body: ResearchRunIn,
                            actor: dict = Depends(require_roles("OPERATOR", "ADMIN"))) -> dict:
    """驱动 harness 跑一轮现场调研。

    入参:
        request: 当前请求。
        version_id: 版本 ID。
        body: 现场答复与可选覆盖问题。
        actor: 已通过 OPERATOR/ADMIN 校验的身份。
    出参:
        统一结构，data 为会话 ID、轮数、结论与 WAIT_CONFIRM 状态。
    失败时:
        状态不符返回 INVALID_STATUS(409)；模型不可用返回 LLM_UNAVAILABLE；事务回滚。
    """
    with db.session(_db_path(request)) as conn:             # 打开连接
        with db.transaction(conn):                         # 调研记录与版本状态变更同事务
            data = service.run_change_research(conn, request.app.state.cfg, actor, version_id, body)  # 执行调研
    return ok(data)                                        # 统一封装


@app.post("/api/v1/changes/{version_id}/research/confirm")  # 人工确认调研结论
async def post_research_confirm(request: Request, version_id: str, body: ResearchConfirmIn,
                                actor: dict = Depends(require_roles("OPERATOR", "ADMIN"))) -> dict:
    """人工确认调研结论（人在回路闸口）。

    入参:
        request: 当前请求。
        version_id: 版本 ID。
        body: confirm 必须为 True，可附备注。
        actor: 已校验身份，确认为具名责任人。
    出参:
        统一结构，data 为 RESEARCH_DONE 状态与确认人。
    失败时:
        未显式确认返回 HUMAN_CONFIRM_REQUIRED；无待确认结论返回 RESEARCH_NOT_READY(409)。
    """
    with db.session(_db_path(request)) as conn:             # 打开连接
        with db.transaction(conn):                         # 确认动作与审计必须原子
            data = service.confirm_research(conn, request.app.state.cfg, actor, version_id, body)  # 执行确认
    return ok(data)                                        # 统一封装


@app.post("/api/v1/changes/{version_id}/submit")           # 提交双人复核
async def post_submit(request: Request, version_id: str,
                      actor: dict = Depends(require_roles("OPERATOR", "ADMIN"))) -> dict:
    """把版本提交到待复核状态。

    入参:
        request: 当前请求。
        version_id: 版本 ID。
        actor: 已校验身份。
    出参:
        统一结构，data 为 PENDING_REVIEW 与提交时间。
    失败时:
        调研未确认返回 RESEARCH_NOT_CONFIRMED(409)；状态不符返回 INVALID_STATUS(409)。
    """
    with db.session(_db_path(request)) as conn:             # 打开连接
        with db.transaction(conn):                         # 状态变更与审计同事务
            data = service.submit_change(conn, request.app.state.cfg, actor, version_id)  # 执行提交
    return ok(data)                                        # 统一封装


@app.post("/api/v1/changes/{version_id}/review")           # 复核（第一/第二人）
async def post_review(request: Request, version_id: str, body: ReviewIn,
                      actor: dict = Depends(require_roles("REVIEWER", "ADMIN"))) -> dict:
    """提交一次复核结论。

    入参:
        request: 当前请求。
        version_id: 版本 ID。
        body: APPROVE/REJECT 与意见。
        actor: 已通过 REVIEWER/ADMIN 校验的身份。
    出参:
        统一结构，data 为复核位、结论与流转后的状态。
    失败时:
        本人复核 REVIEW_SAME_PERSON(409)、重复复核 REVIEW_DUPLICATE(409)、
        状态不符 INVALID_STATUS(409)，全部回滚且不留半成品记录。
    """
    with db.session(_db_path(request)) as conn:             # 打开连接
        with db.transaction(conn):                         # BEGIN IMMEDIATE 抢写锁，防两人同时抢占第二复核位
            data = service.review_change(conn, request.app.state.cfg, actor, version_id, body)  # 执行复核
    return ok(data)                                        # 统一封装


@app.post("/api/v1/changes/{version_id}/effective")        # 生效（不可逆）
async def post_effective(request: Request, version_id: str, body: EffectiveIn,
                         actor: dict = Depends(require_roles("ADMIN"))) -> dict:
    """双人通过后由管理员生效。

    入参:
        request: 当前请求。
        version_id: 版本 ID。
        body: 生效备注。
        actor: 已通过 ADMIN 校验的身份。
    出参:
        统一结构，data 为 EFFECTIVE 状态的脱敏版本视图。
    失败时:
        双签不齐 DUAL_REVIEW_INCOMPLETE(409)、并发冲突 VERSION_CONFLICT(409)，事务回滚。
    """
    with db.session(_db_path(request)) as conn:             # 打开连接
        with db.transaction(conn):                         # 生效与薪酬更新必须原子
            data = service.effective_change(conn, request.app.state.cfg, actor, version_id, body)  # 执行生效
    return ok(data)                                        # 统一封装


@app.get("/api/v1/audit")                                   # 审计流水
async def get_audit(request: Request, limit: int = Query(default=100, ge=1, le=500),
                    actor: dict = Depends(require_roles("ADMIN"))) -> dict:
    """查看审计流水与链完整性。

    入参:
        request: 当前请求。
        limit: 返回条数，1~500。
        actor: 已通过 ADMIN 校验的身份。
    出参:
        统一结构，data 为 chain 校验结果与 items 流水。
    失败时:
        参数越界返回 VALIDATION_ERROR(422)；无权限返回 FORBIDDEN(403)。
    """
    with db.session(_db_path(request)) as conn:             # 只读连接
        data = service.get_audit(conn, request.app.state.cfg, actor, limit)  # 读取并校验
    return ok(data)                                        # 统一封装


@app.get("/api/v1/audit/verify")                            # 单独校验审计链
async def get_audit_verify(request: Request,
                           actor: dict = Depends(require_roles("ADMIN"))) -> dict:
    """只校验审计哈希链是否完整。

    入参:
        request: 当前请求。
        actor: 已通过 ADMIN 校验的身份。
    出参:
        统一结构，data 为 {"ok","count","broken_at"}。
    失败时:
        数据库读取失败返回 INTERNAL_ERROR(500)；链断裂时 data.ok=false 而非接口失败。
    """
    with db.session(_db_path(request)) as conn:             # 只读连接
        data = service.verify_audit(conn)                  # 校验整链
    return ok(data)                                        # 统一封装


@app.get("/", include_in_schema=False)                       # 根路径跳转到前端页面，不进接口文档
async def get_root() -> RedirectResponse:
    """把根路径重定向到前端单页入口。

    入参:
        无。
    出参:
        302 重定向到 /ui。
    失败时:
        不会失败；前端页面本身不可用时由 /ui 返回统一错误结构。
    """
    return RedirectResponse(url="/ui")                     # 固定跳转，页面路径不写在配置里也不含敏感信息


@app.get("/ui", include_in_schema=False)                     # 前端单页入口，同源提供，无需 CORS
async def get_ui(request: Request) -> FileResponse:
    """返回纯静态前端页面。

    入参:
        request: 当前请求，用于读取应用状态中的配置。
    出参:
        text/html 响应，内容为 config.yaml 中 ui.file 指向的单文件页面。
    失败时:
        页面文件不存在时抛 BizError("UI_NOT_FOUND", 500)，由全局处理器转成统一结构。
    """
    path = resolve_path(request.app.state.cfg["ui"]["file"])  # 从配置解析前端文件绝对路径
    if not path.exists():                                  # 文件缺失不能返回空白页蒙混过去
        raise BizError("UI_NOT_FOUND", "前端页面文件不存在", 500)  # 明确失败
    return FileResponse(str(path), media_type="text/html; charset=utf-8")  # 显式声明中文编码
