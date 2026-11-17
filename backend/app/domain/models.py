"""领域实体：用户与会话。

表结构需与 docs/m7-api-auth-design.md §2 保持一致；
迁移由 Alembic 管理，不要手改线上表。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class AppUser(Base):
    """平台用户；LDAP 用户没有本地密码。"""

    __tablename__ = "app_user"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(255))
    ldap_dn: Mapped[str | None] = mapped_column(String(512))
    ldap_groups: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    role_source: Mapped[str] = mapped_column(String(16), default="local")
    password_hash: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSession(Base):
    """登录会话；refresh token 只存哈希。"""

    __tablename__ = "user_session"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(
        String(64), ForeignKey("app_user.username", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    auth_method: Mapped[str] = mapped_column(
        String(16), default="local", server_default=text("'local'")
    )
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserGrant(Base):
    """按人附加的权限，可按数据集限定范围并设置过期时间（M7 §3.3）。"""

    __tablename__ = "user_grant"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(
        String(64), ForeignKey("app_user.username", ondelete="CASCADE"), index=True
    )
    permission: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    reason: Mapped[str | None] = mapped_column(String(512))
    granted_by: Mapped[str] = mapped_column(String(64))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Presence(Base):
    """编辑者在线提示；TTL 到期后不再对其他用户可见。"""

    __tablename__ = "presence"
    __table_args__ = (Index("ix_presence_expires_at", "expires_at"),)

    resource_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    username: Mapped[str] = mapped_column(
        String(64), ForeignKey("app_user.username", ondelete="CASCADE"), primary_key=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ServiceToken(Base):
    """供 CI 调用的受限令牌；只存 secret 哈希且撤销后立即失效（M7 §2.5）。"""

    __tablename__ = "service_token"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    """创建类请求的幂等响应缓存，保留 24 小时（M7 §4.5）。"""

    __tablename__ = "idempotency_key"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, object]] = mapped_column(JSONB)
    status_code: Mapped[int] = mapped_column()
    headers: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    """物理设备；host/port 是网络位置，不参与部署指纹。"""

    __tablename__ = "device"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sn: Mapped[str | None] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(16))  # 205 | 600 | 502
    kind: Mapped[str] = mapped_column(String(16))  # etsstt | gateway | cloud
    host: Mapped[str] = mapped_column(String(128))
    port: Mapped[int] = mapped_column()
    ssh_user: Mapped[str | None] = mapped_column(String(64))
    ssh_port: Mapped[int] = mapped_column(default=22, server_default="22")
    secret_ref: Mapped[str | None] = mapped_column(String(128))
    tags: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(24), default="unknown", server_default="unknown")
    last_error: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by: Mapped[str | None] = mapped_column(String(255))


class DeviceCredential(Base):
    """设备凭据密文；明文只在受控连接调用期间短暂存在（Q7）。"""

    __tablename__ = "device_credential"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), unique=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Deployment(Base):
    """一次冻结的部署快照；uid 只由身份字段计算。"""

    __tablename__ = "deployment"
    __table_args__ = (UniqueConstraint("device_id", "uid", name="uq_deployment_device_uid"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), index=True
    )
    uid: Mapped[str] = mapped_column(String(16))
    platform: Mapped[str | None] = mapped_column(String(16))
    bigversion: Mapped[str | None] = mapped_column(String(64))
    etsllm_version: Mapped[str | None] = mapped_column(String(64))
    accel: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    model_key: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(64))
    quant: Mapped[str | None] = mapped_column(String(32))
    soc: Mapped[str | None] = mapped_column(String(64))
    capabilities: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    limits: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    probe_meta: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    raw: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProbeRun(Base):
    """一次探测记录；lite 只建 WS 发 init，不跑音频。"""

    __tablename__ = "probe_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # ok | failed
    duration_ms: Mapped[int | None] = mapped_column()
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OpsAction(Base):
    """设备运维动作与结果摘要（M2 §2.6）。"""

    __tablename__ = "ops_action"
    __table_args__ = (Index("ix_ops_action_device_created_at", "device_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(16))  # prepare | restart
    actor: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StagingProbe(Base):
    """设备声纹文件暂存能力缓存（M2 §2.5/§5.4）。"""

    __tablename__ = "staging_probe"

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), primary_key=True
    )
    method: Mapped[str] = mapped_column(String(16))
    host_dir: Mapped[str | None] = mapped_column(String(1024))
    container_dir: Mapped[str | None] = mapped_column(String(1024))
    deployment_uid: Mapped[str | None] = mapped_column(String(16))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(1024))


class Dataset(Base):
    """数据集（不可变版本的容器）。"""

    __tablename__ = "dataset"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    source_type: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(String(1024))
    tags: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DatasetVersion(Base):
    """一次导入的结果；版本内容不可变。"""

    __tablename__ = "dataset_version"
    __table_args__ = (UniqueConstraint("dataset_id", "version", name="uq_dataset_version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="importing")
    manifest_key: Mapped[str | None] = mapped_column(String(512))
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(default=0)
    total_duration_ms: Mapped[int] = mapped_column(default=0)
    capabilities: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    stats: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    import_params: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DatasetSubset(Base):
    """版本内的采集条件子集；AISHELL-1 只有 all。"""

    __tablename__ = "dataset_subset"
    __table_args__ = (UniqueConstraint("dataset_version_id", "name", name="uq_subset_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_version.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(32))
    audio_profile: Mapped[str] = mapped_column(String(32), default="as_is")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    item_count: Mapped[int] = mapped_column(default=0)
    total_duration_ms: Mapped[int] = mapped_column(default=0)
    capabilities: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    stats: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class AudioAsset(Base):
    """按 sha256 去重的音频引用；音频本身不拷贝。"""

    __tablename__ = "audio_asset"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    uri: Mapped[str] = mapped_column(String(1024))
    bytes: Mapped[int | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column()
    sample_rate: Mapped[int | None] = mapped_column()
    channels: Mapped[int | None] = mapped_column()
    codec: Mapped[str | None] = mapped_column(String(32))
    origin_path: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AudioVariant(Base):
    """由原始音频按评测条件懒生成的单声道变体（M1 §5.4）。"""

    __tablename__ = "audio_variant"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("audio_asset.id", ondelete="CASCADE"), primary_key=True
    )
    profile: Mapped[str] = mapped_column(String(32), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    uri: Mapped[str] = mapped_column(String(1024))
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Item(Base):
    """一条评测样本：音频 + 参考文本 + 分桶。"""

    __tablename__ = "item"
    __table_args__ = (UniqueConstraint("dataset_version_id", "item_key", name="uq_item_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_version.id", ondelete="CASCADE"), index=True
    )
    subset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_subset.id", ondelete="CASCADE"), index=True
    )
    item_key: Mapped[str] = mapped_column(String(128))
    pair_key: Mapped[str | None] = mapped_column(String(128))
    content_uid: Mapped[str] = mapped_column(String(24), index=True)
    audio_asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("audio_asset.id"))
    split: Mapped[str] = mapped_column(String(32), default="all")
    duration_ms: Mapped[int] = mapped_column(default=0)
    lang: Mapped[str | None] = mapped_column(String(16))
    speakers: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    buckets: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    ref_sha: Mapped[str | None] = mapped_column(String(16))


class RefText(Base):
    """参考文本；P0 只写 verbatim。"""

    __tablename__ = "ref_text"

    item_id: Mapped[int] = mapped_column(
        ForeignKey("item.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    lang: Mapped[str | None] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String(16))


class SpeakerMeta(Base):
    """数据集版本内的说话人属性（M1 §4）。"""

    __tablename__ = "speaker_meta"

    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_version.id", ondelete="CASCADE"), primary_key=True
    )
    speaker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    gender: Mapped[str | None] = mapped_column(String(16))
    age: Mapped[str | None] = mapped_column(String(32))
    accent: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class Speaker(Base):
    """可用于命名评测的说话人及其注册音频集合（M7 §5.1）。"""

    __tablename__ = "speaker"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SpeakerEnrollment(Base):
    """受管注册录音；原始音频写入 ObjectStore，不放进数据库。"""

    __tablename__ = "speaker_enrollment"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    speaker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("speaker.id", ondelete="CASCADE"), index=True
    )
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Voiceprint(Base):
    """按 enrollment 与 deployment 缓存的 192 维 float32 声纹向量（M2 §5.5）。"""

    __tablename__ = "voiceprint"
    __table_args__ = (Index("ix_voiceprint_deployment", "deployment_id"),)

    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("speaker_enrollment.id", ondelete="CASCADE"), primary_key=True
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployment.id", ondelete="CASCADE"), primary_key=True
    )
    dimension: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MetricProfile(Base):
    """口径版本；被 Benchmark 引用后保持不可变。"""

    __tablename__ = "metric_profile"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_metric_profile_name_version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), index=True)
    params: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    profile_hash: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by: Mapped[str | None] = mapped_column(String(255))


class Benchmark(Base):
    """数据集版本 + 口径 + 采样 + 分桶的冻结组合（M3 §2）。

    version_hash 覆盖内容/口径/采样/分桶/参数，相同即视为同一评测定义（§5.6）；
    创建时 hash 冲突按幂等处理：复用已有 Benchmark（§7）。
    """

    __tablename__ = "benchmark"
    __table_args__ = (UniqueConstraint("dataset_version_id", "name", name="uq_benchmark_name"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_version.id", ondelete="CASCADE"), index=True
    )
    metric_profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("metric_profile.id"))
    # 多 subset（如 apy_device_ab 的 phone+mic）；空/["all"] 表示整个版本
    subsets: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    sampling: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    params: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    bucket_rules: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    version_hash: Mapped[str | None] = mapped_column(String(16), index=True)
    note: Mapped[str | None] = mapped_column(String(512))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by: Mapped[str | None] = mapped_column(String(255))


class RunGroup(Base):
    """一次跨多个 Benchmark（通常是多个数据集）发起的评测组。

    一个 Benchmark 只能绑定一个 DatasetVersion，因此多数据集横评需要
    由 RunGroup 聚合多条 Run；组成员关系落在 `run.group_id`。
    """

    __tablename__ = "run_group"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="accuracy")
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    """一次执行；P0 单目标、单路。"""

    __tablename__ = "run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    benchmark_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("benchmark.id"), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run_group.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="accuracy")
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    snapshot: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    auto_score: Mapped[bool] = mapped_column(Boolean, default=True)
    progress: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # ETA（M4 §4/§5）：允许不准，但不允许"假装准"——必须带出处与置信度
    eta_ms: Mapped[int | None] = mapped_column()
    eta_p90_ms: Mapped[int | None] = mapped_column()
    eta_source: Mapped[str | None] = mapped_column(
        String(16)
    )  # deployment|cross_device|kind|fallback|observed
    eta_confidence: Mapped[str | None] = mapped_column(String(8))  # low|medium|high
    created_by: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunTarget(Base):
    """Run 下的一个（deployment, subset）目标（M4 §2.1 / D-M4-9）。

    deployment_id 冻结执行的部署快照（D-M4-11 可锁定 uid，不一致 409）；
    lanes 默认 1（仅 perf 可 >1）；abort_reason 记录熔断/取消原因。
    """

    __tablename__ = "run_target"
    __table_args__ = (UniqueConstraint("run_id", "device_id", "subset", name="uq_target"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("device.id"))
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployment.id", ondelete="RESTRICT"), index=True
    )
    subset: Mapped[str] = mapped_column(String(32), default="all")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    lanes: Mapped[int] = mapped_column(default=1)
    stats: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    abort_reason: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunResourceSample(Base):
    """执行期间设备硬件资源采样（CPU/内存/NPU/温度）；不含任何凭据。"""

    __tablename__ = "run_resource_sample"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_target.id", ondelete="CASCADE"), index=True
    )
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metrics: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class Hypothesis(Base):
    """内容寻址的推理产物（M4 §2.1/§4 / D-M4-12）。

    cache_key = 内容 + 部署 + 参数的哈希；同一 Run 内重试、同音频多 subset
    命中即跳过转写（case.status=cached，无 timing，性能指标不参与）。
    """

    __tablename__ = "hypothesis"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cache_key: Mapped[str] = mapped_column(String(32), unique=True)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployment.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    params_hash: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16))
    chunk_ms: Mapped[int | None] = mapped_column()
    input_rate: Mapped[float | None] = mapped_column()
    concurrency: Mapped[int | None] = mapped_column()
    text: Mapped[str | None] = mapped_column(String)
    segments: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    speaker_match: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    raw_uri: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaseResult(Base):
    """逐样本执行结果；不存指标（指标在 case_score）。"""

    __tablename__ = "case_result"
    __table_args__ = (UniqueConstraint("run_target_id", "item_id", name="uq_case"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_target.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(String)
    wall_ms: Mapped[int | None] = mapped_column()
    # 设备侧推理耗时与 rtf_dev（M4 §2）：分母是 item.duration_ms（有效语音时长），
    # 不用设备自报 usage.rtf（分母口径不对，M5 §5.7）
    server_ms: Mapped[int | None] = mapped_column()
    rtf_dev: Mapped[float | None] = mapped_column()
    # timing 其余段与内容引用（M4 §4）：文本在 hypothesis，可评分不走设备
    hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("hypothesis.id", ondelete="SET NULL")
    )
    dev_ms: Mapped[int | None] = mapped_column()
    ttft_ms: Mapped[int | None] = mapped_column()
    finalize_ms: Mapped[int | None] = mapped_column()
    rtf_e2e: Mapped[float | None] = mapped_column()
    resource: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evaluation(Base):
    """一次可续跑的评分任务。"""

    __tablename__ = "evaluation"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    metric_profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("metric_profile.id"))
    profile_hash: Mapped[str] = mapped_column(String(16))
    scorer_version: Mapped[str] = mapped_column(String(64), default="cer-1", server_default="cer-1")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    progress: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    coverage: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    metrics: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class CaseScore(Base):
    """逐样本指标。"""

    __tablename__ = "case_score"
    __table_args__ = (UniqueConstraint("evaluation_id", "case_result_id", name="uq_score"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation.id", ondelete="CASCADE"), index=True
    )
    case_result_id: Mapped[int] = mapped_column(ForeignKey("case_result.id", ondelete="CASCADE"))
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB)


class MetricValue(Base):
    """聚合指标；报告只读这里，不扫明细（M5 §4）。

    run_target_id 为空表示 evaluation 级汇总；bucket 是 {dim: value} 字典，
    目标/桶级聚合都在评分时落库，报告不再二次计算。
    """

    __tablename__ = "metric_value"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_id",
            "metric",
            "subset",
            "run_target_id",
            "scope",
            "bucket",
            name="uq_metric",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation.id", ondelete="CASCADE"), index=True
    )
    run_target_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run_target.id", ondelete="CASCADE"), index=True
    )
    metric: Mapped[str] = mapped_column(String(32))
    subset: Mapped[str] = mapped_column(String(32), default="all")
    scope: Mapped[str] = mapped_column(String(8), default="micro")  # micro | macro
    bucket: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    value: Mapped[float | None] = mapped_column()
    n: Mapped[int] = mapped_column(default=0)
    ci_low: Mapped[float | None] = mapped_column()
    ci_high: Mapped[float | None] = mapped_column()
    # 指标附加量（P/R、句末准确率、ITN 实体计数等），报告直接读取
    detail: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), default="ok")
    reason: Mapped[str | None] = mapped_column(String(64))


class GatePolicy(Base):
    """评测基线门禁策略（M7 §12）：阈值存数据，运行时不写死。"""

    __tablename__ = "gate_policy"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    benchmark_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("benchmark.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    rules: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    on_missing: Mapped[str] = mapped_column(String(8), default="skip", server_default="skip")
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_by: Mapped[str | None] = mapped_column(String(255))


class RefSegment(Base):
    """参考文本分段（M1 §4）：时间区间 + 说话人，用于 DER/命名与 Badcase 泳道。

    为什么单独建表：长音频（30min 级）分段可达数百条，
    放 Item JSONB 会让列表查询变慢，且分段需要按时间排序/过滤。
    """

    __tablename__ = "ref_segment"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(default=0)
    speaker: Mapped[str] = mapped_column(String(64), default="unknown")
    start_ms: Mapped[int] = mapped_column(default=0)
    end_ms: Mapped[int] = mapped_column(default=0)
    text: Mapped[str] = mapped_column(String)
    skip: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    overlap: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class CaseTag(Base):
    """Badcase 人工打标（M8 §9.7）：1-9 数字标签，一条样本一个标签。

    为什么单独建表而不放 CaseScore：打标是人工判断，重评分不应覆盖；
    打标频率低但需要审计谁标的（created_by）。
    """

    __tablename__ = "case_tag"
    __table_args__ = (UniqueConstraint("case_score_id", name="uq_case_tag"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation.id", ondelete="CASCADE"), index=True
    )
    case_score_id: Mapped[int] = mapped_column(
        ForeignKey("case_score.id", ondelete="CASCADE"), index=True
    )
    tag: Mapped[int] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SseTicket(Base):
    """一次性 SSE stream ticket（M7 §2.4 / D-M7-5）。

    EventSource 无法设 Authorization 头；ticket 60s 有效、用一次即删，
    泄露最多看 60s 只读流。只存哈希，与 refresh token 同策略。
    """

    __tablename__ = "sse_ticket"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    ticket_hash: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column()


class RefreshGrace(Base):
    """refresh 轮换 30s 幂等宽限（M7 §2.2 / D-M7-18）。

    为什么存旧 token 哈希 + 加密的新 token：
    - 宽限期内旧 token 再次出现 → 返回同一个新 token（多标签页并发刷新不互踢）；
    - 宽限期外旧 token 再次出现 → 判定复用，整 family 失效（D-M7-3）。
    新 token 只在服务端短暂存 30s，仍需加密（文档 §2.2"新 token 加密存储 30s"）。
    """

    __tablename__ = "refresh_grace"

    old_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_session.id", ondelete="CASCADE"), index=True
    )
    new_token_enc: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """审计日志；与业务写在同一事务提交（M7 D-M7-11）。

    仅记录管理/运维/认证动作；为什么同事务：不能出现"操作成功但没记录"。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Report(Base):
    """不可变报告快照（M6 §2/§5.8、M7 D-M7-12）。

    payload/html/xlsx 写本地对象目录后落行；快照内容不可变，重建 = 新快照。
    input_hash 记录参与计算的数据指纹，便于判断"同一份数据重复生成"。
    """

    __tablename__ = "report"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16))  # single | matrix | compare | regression
    evaluation_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    query: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    input_hash: Mapped[str] = mapped_column(String(64))
    template_version: Mapped[str] = mapped_column(String(16), default="report-1")
    level: Mapped[str] = mapped_column(String(16), default="summary")  # summary | evidence
    payload_uri: Mapped[str | None] = mapped_column(String(512))
    html_uri: Mapped[str | None] = mapped_column(String(512))
    xlsx_uri: Mapped[str | None] = mapped_column(String(512))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    """统一异步任务资源（M7 §6）：导入、探测与报告共用状态及进度契约。"""

    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_ref_created_at", "ref_type", "ref_id", "created_at"),
        Index("ix_job_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    ref_type: Mapped[str] = mapped_column(String(32))
    ref_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[dict[str, object]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class DeviceLease(Base):
    """设备整机租约；过期可被抢占，进程崩溃后自愈。"""

    __tablename__ = "device_lease"

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), primary_key=True
    )
    run_target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_target.id", ondelete="CASCADE")
    )
    holder: Mapped[str] = mapped_column(String(64))
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
