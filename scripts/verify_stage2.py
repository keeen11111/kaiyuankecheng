"""阶段②验证脚本：验证 FastAPI 启动、统一响应、鉴权、脱敏与知识库检索。"""

import os                                                       # 注入运行期环境变量
import sys                                                      # 把项目根目录加入模块搜索路径
import tempfile                                                 # 创建隔离的临时数据库目录
from pathlib import Path                                        # 处理跨平台路径

ROOT = Path(__file__).resolve().parent.parent                   # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))                                   # 保证能 import app 包
_TMP = tempfile.TemporaryDirectory(prefix="wb_stage2_")       # 临时目录，退出时自动清理
os.environ["APP_TOKEN_SECRET"] = "stage2-only-secret"          # 令牌签名密钥
os.environ["APP_PIN_OPERATOR"] = "pin-op"                      # 经办人口令
os.environ["APP_PIN_REVIEWER_A"] = "pin-a"                     # 第一复核人口令
os.environ["APP_PIN_REVIEWER_B"] = "pin-b"                     # 第二复核人口令
os.environ["APP_PIN_ADMIN"] = "pin-admin"                      # 管理员口令
os.environ["APP_LLM_MODE"] = "mock"                            # 离线模型模式
os.environ["APP_DB_PATH"] = str(Path(_TMP.name) / "stage2.db")  # 临时库

from fastapi.testclient import TestClient                      # 进程内客户端，无需另起服务
from app.main import app                                       # 被测 FastAPI 应用


def check_shape(body: dict) -> None:
    """断言响应体符合统一结构。

    入参:
        body: 响应 JSON 反序列化后的字典。
    出参:
        无返回值。
    失败时:
        结构不符抛 AssertionError，直接判定本阶段失败。
    """
    assert set(body.keys()) == {"ok", "data", "error"}       # 顶层只能有三个键
    assert isinstance(body["ok"], bool)                      # ok 必须是布尔
    assert isinstance(body["error"], dict)                   # error 恒为对象，成功时字段为空串
    assert set(body["error"].keys()) == {"code", "msg"}      # error 只允许两个键
    assert isinstance(body["error"]["code"], str)            # 错误码为字符串
    assert isinstance(body["error"]["msg"], str)             # 提示为字符串


def auth(client: TestClient, actor_id: str, pin: str) -> str:
    """签发令牌并返回 Bearer 头值。

    入参:
        client: TestClient 实例。
        actor_id: 账号 ID。
        pin: 账号口令。
    出参:
        形如 "Bearer xxx" 的字符串。
    失败时:
        签发失败抛 AssertionError，说明环境或配置有问题。
    """
    r = client.post("/api/v1/auth/token", json={"actor_id": actor_id, "pin": pin})  # 调签发接口
    assert r.status_code == 200, r.text                     # 必须成功
    body = r.json()                                        # 解析响应
    check_shape(body)                                      # 校验统一结构
    assert body["ok"] is True                              # 签发成功
    return "Bearer " + body["data"]["token"]               # 返回可直接放进请求头的值


def main() -> None:
    """执行阶段②全部断言。

    入参:
        无。
    出参:
        无返回值，逐条打印 PASS。
    失败时:
        任一断言不成立时抛 AssertionError 并以非零码退出。
    """
    with TestClient(app) as client:                            # 触发 lifespan，完成建表与配置校验
        h = client.get("/health")                             # 探活
        check_shape(h.json())                                # 探活也必须是统一结构
        assert h.status_code == 200 and h.json()["ok"] is True  # 服务健康
        assert h.json()["data"]["llm_mode"] == "mock"        # 离线模式已生效
        assert "SYSTEM_RESEARCH" in h.json()["data"]["prompts_loaded"]  # 提示词文件已加载
        print("[stage2] PASS 启动、配置、提示词加载与 /health")  # 打印通过项

        bad = client.post("/api/v1/auth/token", json={"actor_id": "u_operator", "pin": "wrong"})  # 错误口令
        check_shape(bad.json())                              # 失败也要统一结构
        assert bad.status_code == 401 and bad.json()["error"]["code"] == "UNAUTHORIZED"  # 拒绝且不区分原因
        no_token = client.get("/api/v1/changes")             # 不带令牌访问
        check_shape(no_token.json())                         # 失败也要统一结构
        assert no_token.status_code == 401                   # 必须被依赖拦截
        print("[stage2] PASS 令牌校验与未登录拦截")        # 打印通过项

        op = auth(client, "u_operator", "pin-op")           # 经办人令牌
        ra = auth(client, "u_reviewer_a", "pin-a")          # 复核人令牌
        ad = auth(client, "u_admin", "pin-admin")           # 管理员令牌
        created = client.post("/api/v1/changes", headers={"Authorization": op},  # 创建变更版本
                              json={"emp_id": "E001", "new_base_salary_cents": 2200000,  # 22000.00 元
                                    "reason": "年度绩效调薪，需完成现场取证",  # 书面事由
                                    "research_question": "核实岗位职级、绩效依据与生效月份"})  # 调研问题
        assert created.status_code == 200, created.text    # 创建成功
        body = created.json()                              # 解析响应
        check_shape(body)                                  # 统一结构
        assert body["data"]["status"] == "DRAFT"           # 初始状态为草稿
        assert body["data"]["after"]["base_salary_display"] == "****"  # 经办人看到的是掩码
        assert "base_salary_cents" not in body["data"]["after"]  # 明文分值字段绝不下发
        assert body["data"]["diff"]["direction"] == "MASKED"  # 差额同样脱敏，防反推调薪幅度
        vid = body["data"]["version_id"]                  # 保存版本 ID 供后续断言
        print("[stage2] PASS 版本创建与非管理员薪酬脱敏")  # 打印通过项

        invalid = client.post("/api/v1/changes", headers={"Authorization": op},  # 负数金额
                              json={"emp_id": "E001", "new_base_salary_cents": -1,
                                    "reason": "非法输入", "research_question": "非法输入"})  # 请求体
        check_shape(invalid.json())                        # 校验失败也统一结构
        assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "VALIDATION_ERROR"  # 参数拦截
        forbidden = client.post("/api/v1/changes", headers={"Authorization": ra},  # 复核人越权创建
                                json={"emp_id": "E001", "new_base_salary_cents": 2200000,
                                      "reason": "越权测试", "research_question": "越权测试"})  # 请求体
        check_shape(forbidden.json())                      # 校验统一结构
        assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "FORBIDDEN"  # 角色拦截
        missing = client.get("/api/v1/no-such-route")     # 不存在的路由
        check_shape(missing.json())                        # 404 也要统一结构
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "HTTP_ERROR"  # 框架异常已转换
        print("[stage2] PASS 参数校验、角色越权拦截与 404 统一结构")  # 打印通过项

        admin_view = client.get(f"/api/v1/changes/{vid}", headers={"Authorization": ad})  # 管理员看详情
        assert admin_view.status_code == 200               # 查询成功
        assert admin_view.json()["data"]["after"]["base_salary_cents"] == 2200000  # 管理员可见明文整数
        kb_hit = client.get("/api/v1/kb/search?q=薪酬调整审批规范",  # 检索制度知识库
                            headers={"Authorization": op})  # 经办人可读
        assert kb_hit.status_code == 200 and kb_hit.json()["ok"] is True  # 检索成功
        assert len(kb_hit.json()["data"]["hits"]) >= 1    # 种子文档必须能被检索到
        audit_denied = client.get("/api/v1/audit", headers={"Authorization": op})  # 经办人越权看审计
        assert audit_denied.status_code == 403            # 被拒绝
        audit = client.get("/api/v1/audit", headers={"Authorization": ad})  # 管理员看审计
        assert audit.status_code == 200 and audit.json()["data"]["chain"]["ok"] is True  # 链完整
        verify = client.get("/api/v1/audit/verify", headers={"Authorization": ad})  # 单独校验链
        assert verify.json()["data"]["ok"] is True        # 未被篡改
        print("[stage2] PASS 管理员明文视图、知识库检索与审计链")  # 打印通过项

    _TMP.cleanup()                                         # 清理临时数据库目录
    print("[stage2] ALL PASS")                             # 全部通过


if __name__ == "__main__":                                  # 只在直接执行时运行
    main()                                                  # 调用主函数
