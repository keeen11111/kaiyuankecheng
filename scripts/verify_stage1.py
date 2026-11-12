"""阶段①验证脚本：验证配置加载、表结构、数据库级双人约束与审计哈希链。"""

import os                                                       # 注入运行期环境变量
import sqlite3                                                  # 捕获预期的数据库约束异常
import sys                                                      # 把项目根目录加入模块搜索路径
import tempfile                                                 # 创建隔离的临时数据库目录
from pathlib import Path                                        # 处理跨平台路径

ROOT = Path(__file__).resolve().parent.parent                   # 项目根目录：scripts/ 的上一级
sys.path.insert(0, str(ROOT))                                   # 保证能 import app 包，无需先安装包
_TMP = tempfile.TemporaryDirectory(prefix="wb_stage1_")       # 临时目录，退出时自动清理
os.environ["APP_TOKEN_SECRET"] = "stage1-only-secret"          # 令牌签名密钥，只在本进程内有效
os.environ["APP_PIN_OPERATOR"] = "pin-op"                      # 经办人口令
os.environ["APP_PIN_REVIEWER_A"] = "pin-a"                     # 第一复核人口令
os.environ["APP_PIN_REVIEWER_B"] = "pin-b"                     # 第二复核人口令
os.environ["APP_PIN_ADMIN"] = "pin-admin"                      # 管理员口令
os.environ["APP_LLM_MODE"] = "mock"                            # 离线模型模式，无需启动千问服务
os.environ["APP_DB_PATH"] = str(Path(_TMP.name) / "stage1.db")  # 临时库，避免污染正式数据

from app.audit import append_audit, verify_chain               # 审计写入与链校验
from app.config import load_config                             # 配置加载
from app.db import dumps, init_db, new_id, now_utc, session, transaction  # 数据库初始化与工具函数


def main() -> None:
    """执行阶段①全部断言。

    入参:
        无。
    出参:
        无返回值，逐条打印 PASS。
    失败时:
        任一断言不成立时抛 AssertionError 并以非零码退出，便于 CI 直接判定失败。
    """
    cfg = load_config()                                        # 加载并校验 config.yaml
    assert cfg["llm"]["mode"] == "mock"                       # 确认已被环境变量覆盖为离线模式
    assert cfg["security"]["token_secret"] == "stage1-only-secret"  # 确认 ${VAR} 占位符已解析
    print("[stage1] PASS 配置加载与环境变量注入")            # 打印通过项

    info = init_db(cfg)                                       # 建表、触发器、全文索引、种子数据
    assert info["fts"] is True                               # 当前 SQLite 支持 trigram 全文索引
    with session(info["db_path"]) as conn:                   # 打开只读检查连接
        tables = {r["name"] for r in conn.execute(          # 取出全部表名
            "SELECT name FROM sqlite_master WHERE type='table'")}  # 查询元数据
        required = {"employees", "salary_records", "salary_change_versions",  # 必备表集合
                    "review_records", "research_sessions", "kb_docs", "audit_log", "kb_fts"}  # 继续集合
        assert required <= tables                           # 少任何一张表都算失败
        triggers = {r["name"] for r in conn.execute(        # 取出全部触发器名
            "SELECT name FROM sqlite_master WHERE type='trigger'")}  # 查询元数据
        assert "trg_review_no_self" in triggers             # 数据库层的"自己批自己"拦截必须存在
        seed = conn.execute(                               # 查种子员工
            "SELECT emp_id,name_mask FROM employees WHERE emp_id='E001'"  # 固定工号
        ).fetchone()                                       # 取一行
        assert seed is not None and "*" in seed["name_mask"]  # 员工姓名必须是脱敏的
    print("[stage1] PASS 表结构、触发器、FTS5 索引与脱敏种子数据")  # 打印通过项

    with session(info["db_path"]) as conn:                   # 打开约束验证连接
        vid = new_id("ver")                                 # 造一个测试版本 ID
        conn.execute(                                      # 插入一条草稿版本，供复核约束测试使用
            "INSERT INTO salary_change_versions(version_id,emp_id,from_version_no,before_json,after_json,"
            "reason,operator_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",  # 占位符
            (vid, "E001", 1, dumps({"base_salary_cents": 2000000}),  # 变更前快照
             dumps({"base_salary_cents": 2200000}), "约束测试", "u_operator", "DRAFT", now_utc()),  # 各字段
        )                                                  # 插入结束
        conn.execute(                                      # 第一复核人正常写入
            "INSERT INTO review_records(review_id,version_id,reviewer_id,slot,decision,comment,created_at) "
            "VALUES(?,?,?,?,?,?,?)",                      # 占位符
            (new_id("rev"), vid, "u_reviewer_a", 1, "APPROVE", "", now_utc()),  # 绑定
        )                                                  # 插入结束
        try:                                               # 同一人复核第二次应被唯一约束拦截
            conn.execute(                                  # 尝试以相同 reviewer 写第二个复核位
                "INSERT INTO review_records(review_id,version_id,reviewer_id,slot,decision,comment,created_at) "
                "VALUES(?,?,?,?,?,?,?)",                  # 占位符
                (new_id("rev"), vid, "u_reviewer_a", 2, "APPROVE", "", now_utc()),  # 绑定
            )                                              # 插入结束
            raise AssertionError("同一人复核两次未被拦截")  # 走到这里说明约束失效
        except sqlite3.IntegrityError:                      # 预期的唯一约束冲突
            pass                                           # 约束生效，继续
        try:                                               # 经办人复核自己应被触发器拦截
            conn.execute(                                  # 尝试以 operator 身份写第二个复核位
                "INSERT INTO review_records(review_id,version_id,reviewer_id,slot,decision,comment,created_at) "
                "VALUES(?,?,?,?,?,?,?)",                  # 占位符
                (new_id("rev"), vid, "u_operator", 2, "APPROVE", "", now_utc()),  # 绑定
            )                                              # 插入结束
            raise AssertionError("经办人自复核未被触发器拦截")  # 走到这里说明触发器失效
        except sqlite3.IntegrityError:                      # 预期的 RAISE(ABORT)
            pass                                           # 触发器生效，继续
        try:                                               # 第三个复核位应被 slot 唯一约束拦截
            conn.execute(                                  # 尝试复用已被占用的 slot=1
                "INSERT INTO review_records(review_id,version_id,reviewer_id,slot,decision,comment,created_at) "
                "VALUES(?,?,?,?,?,?,?)",                  # 占位符
                (new_id("rev"), vid, "u_reviewer_b", 1, "APPROVE", "", now_utc()),  # 绑定
            )                                              # 插入结束
            raise AssertionError("复核位重复未被拦截")    # 走到这里说明约束失效
        except sqlite3.IntegrityError:                      # 预期的唯一约束冲突
            pass                                           # 约束生效，继续
    print("[stage1] PASS 双人不同人、一人一次、最多两签的数据库硬约束")  # 打印通过项

    with session(info["db_path"]) as conn:                   # 打开审计链验证连接
        with transaction(conn):                            # 审计写入放在显式事务里
            append_audit(conn, "u_operator", "CHANGE_CREATE", vid,  # 写一条含敏感金额的载荷
                         {"base_salary_cents": 2200000, "reason": "约束测试"})  # 金额应被清洗
            append_audit(conn, "u_reviewer_a", "REVIEW_APPROVE_ONE", vid, {})  # 写第二条
        chain = verify_chain(conn)                         # 校验整链
        assert chain["ok"] is True and chain["count"] == 2  # 两条记录且链完整
        row = conn.execute(                                # 取回刚写的审计载荷摘要
            "SELECT payload_hash FROM audit_log ORDER BY seq ASC LIMIT 1"  # 第一条
        ).fetchone()                                       # 取一行
        assert "2200000" not in str(row["payload_hash"])   # 摘要里不可能出现明文金额
        conn.execute("UPDATE audit_log SET action='TAMPERED' WHERE seq=1")  # 人为篡改一条记录
        broken = verify_chain(conn)                        # 再次校验
        assert broken["ok"] is False and broken["broken_at"] == 1  # 必须能定位到被改的那条
    print("[stage1] PASS 审计哈希链可校验、可定位篡改点")  # 打印通过项

    _TMP.cleanup()                                         # 清理临时数据库目录
    print("[stage1] ALL PASS")                             # 全部通过


if __name__ == "__main__":                                  # 只在直接执行时运行
    main()                                                  # 调用主函数
