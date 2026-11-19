"""请求体模型：所有外部输入的字段、类型与取值范围都在这里收敛。"""

from pydantic import BaseModel, Field                          # BaseModel 定义结构，Field 提供约束与说明


class TokenIn(BaseModel):
    """签发令牌的请求体。"""                                   # 类说明

    actor_id: str = Field(min_length=1, max_length=64)         # 账号 ID，限制长度防止超长输入
    pin: str = Field(min_length=1, max_length=128)              # 明文口令，仅用于本次校验，不落库


class ChangeCreateIn(BaseModel):
    """创建薪酬变更版本的请求体。"""                           # 类说明

    emp_id: str = Field(min_length=1, max_length=64)           # 员工工号
    new_base_salary_cents: int = Field(gt=0, le=10000000000)  # 变更后月薪（分），必须为正且设上限，防误填
    reason: str = Field(min_length=4, max_length=500)          # 变更事由，太短视为无效，强制书面留痕
    research_question: str = Field(min_length=4, max_length=500)  # 现场调研的核心问题


class ChangeCloneIn(BaseModel):
    """基于已有版本重建草稿的请求体（驳回后重提的标准路径）。"""  # 类说明

    new_base_salary_cents: int | None = Field(default=None, gt=0, le=10000000000)  # 新金额，留空则沿用原版本金额
    reason: str | None = Field(default=None, max_length=500)   # 新事由，留空则由服务层在原事由后追加说明
    research_question: str | None = Field(default=None, max_length=500)  # 新调研问题，留空则沿用原问题


class ReviewIn(BaseModel):
    """提交复核结论的请求体。"""                               # 类说明

    decision: str = Field(pattern="^(APPROVE|REJECT)$")       # 结论只能是 APPROVE 或 REJECT，正则在服务层之前先拦
    comment: str = Field(default="", max_length=500)           # 复核意见，可为空但必须书面留痕时由业务层再判


class ResearchRunIn(BaseModel):
    """启动现场调研的请求体。"""                               # 类说明

    field_answers: dict[str, str] = Field(default_factory=dict)  # 现场人员对问题的答复，键为问题、值为答复
    question: str | None = Field(default=None, max_length=500)  # 可选覆盖调研问题，为空则用版本创建时的问题


class ResearchConfirmIn(BaseModel):
    """人工确认调研结论的请求体。"""                           # 类说明

    confirm: bool = Field(default=False)                       # 必须显式为 True 才采纳，防止误触即生效
    note: str = Field(default="", max_length=500)              # 确认备注，写入审计


class KBAddIn(BaseModel):
    """知识库文档入库请求体。"""                               # 类说明

    title: str = Field(min_length=1, max_length=200)           # 文档标题
    content: str = Field(min_length=1, max_length=20000)       # 文档正文，限制长度防止撑爆全文索引
    source: str = Field(default="internal-policy", max_length=100)  # 来源标识


class EffectiveIn(BaseModel):
    """变更生效请求体。"""                                     # 类说明

    note: str = Field(default="", max_length=500)              # 生效备注，写入审计
