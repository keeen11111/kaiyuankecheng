"""阶段③验证脚本：跑通 调研→人工确认→提交→双人复核→生效 全链路，并验证所有拦截点。"""

import os                                                       # 注入运行期环境变量
import sys                                                      # 把项目根目录加入模块搜索路径
import tempfile                                                 # 创建隔离的临时数据库目录
from pathlib import Path                                        # 处理跨平台路径

ROOT = Path(__file__).resolve().parent.parent                   # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))                                   # 保证能 import app 包
_TMP = tempfile.TemporaryDirectory(prefix="wb_stage3_")       # 临时目录，退出时自动清理
os.environ["APP_TOKEN_SECRET"] = "stage3-only-secret"          # 令牌签名密钥
os.environ["APP_PIN_OPERATOR"] = "pin-op"                      # 经办人口令
os.environ["APP_PIN_REVIEWER_A"] = "pin-a"                     # 第一复核人口令
os.environ["APP_PIN_REVIEWER_B"] = "pin-b"                     # 第二复核人口令
os.environ["APP_PIN_ADMIN"] = "pin-admin"                      # 管理员口令
os.environ["APP_LLM_MODE"] = "mock"                            # 离线模型模式，验证脚本无需真实千问
os.environ["APP_DB_PATH"] = str(Path(_TMP.name) / "stage3.db")  # 临时库

from fastapi.testclient import TestClient                      # 进程内客户端
from app.config import load_config, resolve_path              # 读取配置与数据库绝对路径
from app.db import session                                    # 直连数据库核对最终薪酬
from app.main import app                                       # 被测 FastAPI 应用

FIELD_ANSWERS = {                                              # 现场书面答复，键与 mock 模型的提问一致
    "请确认调薪生效月份、绩效书面依据及员工是否知悉": "生效月份为次月，绩效结论有书面评级表，员工已签字知悉。",  # 答复
}                                                              # 字典结束


def auth(client: TestClient, actor_id: str, pin: str) -> str:
    """签发令牌并返回 Bearer 头值。

    入参:
        client: TestClient 实例。
        actor_id: 账号 ID。
        pin: 账号口令。
    出参:
        "Bearer xxx" 字符串。
    失败时:
        签发失败抛 AssertionError。
    """
    r = client.post("/api/v1/auth/token", json={"actor_id": actor_id, "pin": pin})  # 调签发接口
    assert r.status_code == 200 and r.json()["ok"] is True, r.text  # 必须成功
    return "Bearer " + r.json()["data"]["token"]           # 返回请求头值


def create(client: TestClient, token: str, cents: int, reason: str) -> str:
    """创建一个变更版本并返回 ID。

    入参:
        client: TestClient 实例。
        token: Bearer 头值。
        cents: 变更后月薪（分）。
        reason: 变更事由。
    出参:
        版本 ID 字符串。
    失败时:
        创建失败抛 AssertionError 并带响应体，便于定位。
    """
    r = client.post("/api/v1/changes", headers={"Authorization": token},  # 调创建接口
                    json={"emp_id": "E001", "new_base_salary_cents": cents,  # 请求体
                          "reason": reason,                 # 事由
                          "research_question": "核实岗位职级、绩效依据与生效月份"})  # 调研问题
    assert r.status_code == 200 and r.json()["ok"] is True, r.text  # 创建成功
    return r.json()["data"]["version_id"]                  # 返回版本 ID


def prepare(client: TestClient, token: str, cents: int, reason: str) -> str:
    """走完调研、人工确认、提交三步，把版本送到待复核状态。

    入参:
        client: TestClient 实例。
        token: Bearer 头值。
        cents: 变更后月薪（分）。
        reason: 变更事由。
    出参:
        处于 PENDING_REVIEW 的版本 ID。
    失败时:
        任一步失败抛 AssertionError，说明链路被改坏。
    """
    vid = create(client, token, cents, reason)             # 先创建草稿
    r1 = client.post(f"/api/v1/changes/{vid}/research/run",  # 跑 harness
                     headers={"Authorization": token},     # 带令牌
                     json={"field_answers": FIELD_ANSWERS})  # 传现场答复
    assert r1.status_code == 200 and r1.json()["ok"] is True, r1.text  # 调研成功
    r2 = client.post(f"/api/v1/changes/{vid}/research/confirm",  # 人工确认
                     headers={"Authorization": token}, json={"confirm": True, "note": "已核对书面材料"})  # 显式确认
    assert r2.status_code == 200 and r2.json()["ok"] is True, r2.text  # 确认成功
    r3 = client.post(f"/api/v1/changes/{vid}/submit", headers={"Authorization": token})  # 提交复核
    assert r3.status_code == 200 and r3.json()["ok"] is True, r3.text  # 提交成功
    return vid                                             # 返回待复核版本


def main() -> None:
    """执行阶段③全部断言。

    入参:
        无。
    出参:
        无返回值，逐条打印 PASS。
    失败时:
        任一断言不成立时抛 AssertionError 并以非零码退出。
    """
    with TestClient(app) as client:                            # 触发 lifespan
        op = auth(client, "u_operator", "pin-op")            # 经办人令牌
        ra = auth(client, "u_reviewer_a", "pin-a")           # 第一复核人令牌
        rb = auth(client, "u_reviewer_b", "pin-b")           # 第二复核人令牌
        ad = auth(client, "u_admin", "pin-admin")            # 管理员令牌

        v1 = create(client, op, 2200000, "年度绩效调薪，需完成现场取证")  # 主链路版本
        run = client.post(f"/api/v1/changes/{v1}/research/run",  # 跑现场调研
                          headers={"Authorization": op},    # 经办人发起
                          json={"field_answers": FIELD_ANSWERS})  # 现场答复
        assert run.status_code == 200 and run.json()["ok"] is True, run.text  # 调研成功
        data = run.json()["data"]                           # 取调研结果
        assert data["status"] == "WAIT_CONFIRM"             # 结论待人工确认，不能自动进复核
        assert data["version_status"] == "DRAFT"            # 版本仍停在草稿
        assert data["rounds"] == 3                          # 检索、取证、收尾共三轮
        for key in ("summary", "findings", "evidence", "risk_level", "recommend"):  # 结论必需字段
            assert key in data["conclusion"]                # 逐个确认存在
        tools = [t["model_call"]["tool"] for t in data["transcript"]]  # 从调研轨迹取模型选过的工具
        assert tools == ["kb_search", "ask_field", "finish"]  # 检索→取证→收尾，顺序固定且全在白名单内
        print("[stage3] PASS harness 多轮调研产出待确认结论")  # 打印通过项

        reject_confirm = client.post(f"/api/v1/changes/{v1}/research/confirm",  # 未显式确认
                                     headers={"Authorization": op}, json={"confirm": False})  # 请求体
        assert reject_confirm.status_code == 400 and reject_confirm.json()["error"]["code"] == "HUMAN_CONFIRM_REQUIRED"  # 被拦
        early_submit = client.post(f"/api/v1/changes/{v1}/submit", headers={"Authorization": op})  # 未确认就提交
        assert early_submit.status_code == 409 and early_submit.json()["error"]["code"] == "INVALID_STATUS"  # 被拦
        confirm = client.post(f"/api/v1/changes/{v1}/research/confirm",  # 正确的人工确认
                              headers={"Authorization": op},
                              json={"confirm": True, "note": "已核对书面评级表"})  # 备注
        assert confirm.status_code == 200 and confirm.json()["data"]["status"] == "RESEARCH_DONE"  # 状态推进
        assert confirm.json()["data"]["confirmed_by"] == "u_operator"  # 确认人具名
        submit = client.post(f"/api/v1/changes/{v1}/submit", headers={"Authorization": op})  # 提交复核
        assert submit.status_code == 200 and submit.json()["data"]["status"] == "PENDING_REVIEW"  # 待第一人
        print("[stage3] PASS 人工确认闸口：未确认不能提交复核")  # 打印通过项

        v2 = prepare(client, ad, 2300000, "管理员经办，用于验证自复核拦截")  # ADMIN 同时是经办人
        self_review = client.post(f"/api/v1/changes/{v2}/review",  # 管理员复核自己经办的版本
                                  headers={"Authorization": ad}, json={"decision": "APPROVE", "comment": ""})  # 请求体
        assert self_review.status_code == 409                  # 冲突
        assert self_review.json()["error"]["code"] == "REVIEW_SAME_PERSON"  # 服务层给出可读错误码
        print("[stage3] PASS 经办人自复核被服务层拦截")      # 打印通过项

        one = client.post(f"/api/v1/changes/{v1}/review", headers={"Authorization": ra},  # 第一人通过
                          json={"decision": "APPROVE", "comment": "材料齐备"})  # 复核意见
        assert one.status_code == 200 and one.json()["data"]["status"] == "REVIEW_ONE"  # 等第二人
        dup = client.post(f"/api/v1/changes/{v1}/review", headers={"Authorization": ra},  # 同一人再签
                          json={"decision": "APPROVE", "comment": "重复"})  # 请求体
        assert dup.status_code == 409 and dup.json()["error"]["code"] == "REVIEW_DUPLICATE"  # 被拦
        early_effective = client.post(f"/api/v1/changes/{v1}/effective",  # 只有一签就想生效
                                      headers={"Authorization": ad}, json={"note": "提前生效"})  # 请求体
        assert early_effective.status_code == 409 and early_effective.json()["error"]["code"] == "INVALID_STATUS"  # 被拦
        print("[stage3] PASS 一人一次与单签禁止生效")        # 打印通过项

        two = client.post(f"/api/v1/changes/{v1}/review", headers={"Authorization": rb},  # 第二人通过
                          json={"decision": "APPROVE", "comment": "同意"})  # 复核意见
        assert two.status_code == 200 and two.json()["data"]["status"] == "APPROVED"  # 双人齐备
        no_right = client.post(f"/api/v1/changes/{v1}/effective", headers={"Authorization": op},  # 经办人越权生效
                               json={"note": "越权"})       # 请求体
        assert no_right.status_code == 403                  # 被角色依赖拦截
        eff = client.post(f"/api/v1/changes/{v1}/effective", headers={"Authorization": ad},  # 管理员生效
                          json={"note": "双人复核通过，按流程生效"})  # 备注
        assert eff.status_code == 200 and eff.json()["data"]["status"] == "EFFECTIVE"  # 生效成功
        assert eff.json()["data"]["after"]["base_salary_cents"] == 2200000  # 管理员视图可见明文
        view = client.get(f"/api/v1/changes/{v1}", headers={"Authorization": ra})  # 复核人视图
        assert view.json()["data"]["after"]["base_salary_display"] == "****"  # 生效后依然脱敏
        assert len(view.json()["data"]["reviews"]) == 2    # 恰好两条复核轨迹
        print("[stage3] PASS 双人复核通过后由管理员原子生效")  # 打印通过项

        cfg = load_config()                                # 读取配置拿数据库路径
        with session(str(resolve_path(cfg["db"]["path"]))) as conn:  # 直连数据库核对落库结果
            rec = conn.execute(                            # 查当前生效薪酬
                "SELECT base_salary_cents,version_no FROM salary_records WHERE emp_id='E001'"  # 固定工号
            ).fetchone()                                   # 取一行
            assert rec["base_salary_cents"] == 2200000    # 金额已真正更新
            assert rec["version_no"] == 2                 # 乐观锁版本号递增
        print("[stage3] PASS 薪酬基线与版本号已原子更新")  # 打印通过项

        v3 = prepare(client, op, 2400000, "验证驳回分支")  # 再造一个待复核版本
        rejected = client.post(f"/api/v1/changes/{v3}/review", headers={"Authorization": ra},  # 第一人驳回
                               json={"decision": "REJECT", "comment": "缺少书面绩效依据"})  # 意见
        assert rejected.status_code == 200 and rejected.json()["data"]["status"] == "REJECTED"  # 终止
        after_reject = client.post(f"/api/v1/changes/{v3}/review", headers={"Authorization": rb},  # 驳回后再复核
                                   json={"decision": "APPROVE", "comment": ""})  # 请求体
        assert after_reject.status_code == 409 and after_reject.json()["error"]["code"] == "INVALID_STATUS"  # 被拦
        print("[stage3] PASS 驳回后状态机不可再流转")      # 打印通过项

        audit = client.get("/api/v1/audit?limit=200", headers={"Authorization": ad})  # 拉取审计流水
        assert audit.status_code == 200 and audit.json()["data"]["chain"]["ok"] is True  # 链完整
        actions = {item["action"] for item in audit.json()["data"]["items"]}  # 动作集合
        for expected in ("CHANGE_CREATE", "RESEARCH_RUN", "RESEARCH_CONFIRM", "CHANGE_SUBMIT",  # 关键动作
                         "REVIEW_APPROVE_ONE", "REVIEW_APPROVE_TWO", "CHANGE_EFFECTIVE", "REVIEW_REJECT"):  # 继续
            assert expected in actions                    # 每个动作都必须留痕
        print("[stage3] PASS 全链路审计留痕与哈希链完整")  # 打印通过项

    _TMP.cleanup()                                         # 清理临时数据库目录
    print("[stage3] ALL PASS")                             # 全部通过


if __name__ == "__main__":                                  # 只在直接执行时运行
    main()                                                  # 调用主函数
