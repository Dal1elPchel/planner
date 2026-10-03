"""SQLAlchemy 2.0 модели: единственный источник истины по схеме БД.

Куда положить: backend/core/models.py

Правила:
- Схему меняем только здесь + миграция Alembic (autogenerate, затем ручная проверка).
- Все время в БД хранится как timestamptz (UTC). Календарные даты (due_date,
  checked_on, entry_date, for_date) считаются по часовому поясу пользователя
  в слое фасадов, а в БД лежат как обычный DATE.
- Лимиты длины продублированы здесь (БД) и в Pydantic-схемах (API).
  Числовые квоты (сколько задач на пользователя) живут в core/limits.py.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    MetaData,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Явные имена констрейнтов: без них Alembic генерирует нестабильные имена
# и миграции на удаление/изменение ограничений становятся болью.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------- #
# Enums (в БД хранятся строками + CHECK, не нативными PG enum: проще мигрировать)
# --------------------------------------------------------------------------- #


def _enum(py_enum: type[enum.Enum], name: str) -> SAEnum:
    return SAEnum(
        py_enum,
        name=name,
        native_enum=False,
        length=20,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


class TaskStatus(str, enum.Enum):
    ACTIVE = "active"
    DONE = "done"
    CANCELLED = "cancelled"


class HabitPeriod(str, enum.Enum):
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class ReflectionKind(str, enum.Enum):
    TOPIC = "topic"  # заметка на тему («5 моих достоинств»)
    DAILY_REVIEW = "daily_review"  # вечерние итоги дня


class TopicSource(str, enum.Enum):
    STATIC = "static"  # зашитый резервный список
    LLM = "llm"  # сгенерировано ChatGPT


class NotificationKind(str, enum.Enum):
    MORNING = "morning"
    EVENING = "evening"
    DEADLINE = "deadline"


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"  # пользователь заблокировал бота


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))
    # Уровень пользователя (поле из прототипа; механика пока не определена)
    level: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default=text("1")
    )

    # IANA-имя ("Europe/Paris"). Валидируется через zoneinfo в слое схем/фасада.
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC"
    )
    morning_time: Mapped[time] = mapped_column(
        Time, default=time(8, 0), server_default=text("'08:00'")
    )
    evening_time: Mapped[time] = mapped_column(
        Time, default=time(21, 0), server_default=text("'21:00'")
    )
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    # false, если бот заблокирован пользователем (TelegramForbiddenError)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )

    __table_args__ = (
        CheckConstraint("level >= 1", name="level_positive"),
        Index("ix_users_active_notifications", "is_active", "notifications_enabled"),
    )


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))

    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[TaskStatus] = mapped_column(
        _enum(TaskStatus, "task_status"),
        default=TaskStatus.ACTIVE,
        server_default=TaskStatus.ACTIVE.value,
    )
    # Матрица Эйзенхауэра: срочность и важность, 1 (низкая) .. 3 (высокая)
    urgency: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default=text("1")
    )
    importance: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default=text("1")
    )
    deadline: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("length(btrim(title)) > 0", name="title_not_blank"),
        # completed_at заполнен тогда и только тогда, когда задача выполнена
        CheckConstraint(
            "(status = 'done') = (completed_at IS NOT NULL)",
            name="done_has_completed_at",
        ),
        CheckConstraint("urgency BETWEEN 1 AND 3", name="urgency_range"),
        CheckConstraint("importance BETWEEN 1 AND 3", name="importance_range"),
        Index("ix_tasks_user_status", "user_id", "status"),
        Index("ix_tasks_user_deadline", "user_id", "deadline"),
        # для шедулера: активные задачи по дате дедлайна
        Index(
            "ix_tasks_deadline_active",
            "deadline",
            postgresql_where=text("status = 'active'"),
        ),
    )


# --------------------------------------------------------------------------- #
# Habits
# --------------------------------------------------------------------------- #


class Habit(TimestampMixin, Base):
    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))

    title: Mapped[str] = mapped_column(String(100))
    # «15 тренировок в месяц»: target=15, period=month => 15 ячеек в таблице.
    # Потолок target зависит от периода (7 / 31 / 366), см. CheckConstraint ниже.
    target: Mapped[int] = mapped_column(SmallInteger)
    period: Mapped[HabitPeriod] = mapped_column(_enum(HabitPeriod, "habit_period"))
    color: Mapped[str] = mapped_column(
        String(7), default="#6C63FF", server_default="#6C63FF"
    )
    archived: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    # lazy="raise": в async-коде запрещаем неявные ленивые загрузки.
    # Нужны checks -> грузим явно (selectinload) в репозитории.
    checks: Mapped[list[HabitCheck]] = relationship(
        back_populates="habit",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint("length(btrim(title)) > 0", name="title_not_blank"),
        CheckConstraint(
            "target BETWEEN 1 AND "
            "CASE period WHEN 'week' THEN 7 WHEN 'month' THEN 31 ELSE 366 END",
            name="target_within_period",
        ),
        CheckConstraint("color ~ '^#[0-9A-Fa-f]{6}$'", name="color_hex"),
        Index("ix_habits_user_archived", "user_id", "archived"),
    )


class HabitCheck(Base):
    """Одна поставленная галочка = одна запись с датой.

    UNIQUE(habit_id, checked_on) даёт идемпотентность (повторный клик не создаёт
    дубль) и естественный потолок: не больше одной галочки в день.
    Сколько ячеек рисовать в таблице — берётся из habit.target,
    сколько закрашивать — из числа записей в текущем окне периода
    (неделя / месяц / год, считается в фасаде).
    """

    __tablename__ = "habit_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    habit_id: Mapped[int] = mapped_column(
        ForeignKey("habits.id", ondelete="CASCADE")
    )
    checked_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    habit: Mapped[Habit] = relationship(back_populates="checks", lazy="raise")

    __table_args__ = (
        UniqueConstraint("habit_id", "checked_on", name="uq_habit_checks_habit_day"),
    )


# --------------------------------------------------------------------------- #
# Reflections
# --------------------------------------------------------------------------- #


class Reflection(TimestampMixin, Base):
    __tablename__ = "reflections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))

    kind: Mapped[ReflectionKind] = mapped_column(
        _enum(ReflectionKind, "reflection_kind"),
        default=ReflectionKind.TOPIC,
        server_default=ReflectionKind.TOPIC.value,
    )
    topic: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    mood: Mapped[int | None] = mapped_column(SmallInteger)  # 1..5, необязательно
    # Локальная (по таймзоне пользователя) дата, к которой относится запись.
    entry_date: Mapped[date] = mapped_column(Date)

    __table_args__ = (
        CheckConstraint("length(body) <= 5000", name="body_max_length"),
        CheckConstraint("mood IS NULL OR mood BETWEEN 1 AND 5", name="mood_range"),
        CheckConstraint(
            "kind <> 'topic' OR topic IS NOT NULL", name="topic_required_for_topic"
        ),
        Index("ix_reflections_user_date", "user_id", "entry_date"),
        # Не больше одних «итогов дня» в сутки на пользователя
        Index(
            "uq_reflections_daily_review_per_day",
            "user_id",
            "entry_date",
            unique=True,
            postgresql_where=text("kind = 'daily_review'"),
        ),
    )


class ReflectionTopic(Base):
    """Общий пул тем для рефлексии (не привязан к пользователю).

    Заполняется: 1) seed-скриптом со статическим списком (резерв),
    2) периодической генерацией через ChatGPT. Случайная тема выбирается из пула,
    поэтому падение LLM API ничего не ломает.
    """

    __tablename__ = "reflection_topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    prompt: Mapped[str] = mapped_column(String(200), unique=True)
    source: Mapped[TopicSource] = mapped_column(
        _enum(TopicSource, "topic_source"),
        default=TopicSource.STATIC,
        server_default=TopicSource.STATIC.value,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("length(btrim(prompt)) > 0", name="prompt_not_blank"),
    )


# --------------------------------------------------------------------------- #
# Notifications (идемпотентность шедулера)
# --------------------------------------------------------------------------- #


class NotificationLog(Base):
    """Журнал отправленных напоминаний.

    Алгоритм (защита от дублей при рестартах и гонках):
      1. INSERT ... ON CONFLICT DO NOTHING (status=pending) по ключу
         (user_id, kind, for_date).
      2. Если строка вставилась, значит, мы «владеем» отправкой: шлём сообщение.
      3. Обновляем status = sent / failed / blocked.
    for_date: локальная дата пользователя, на которую приходится напоминание.
    Записи старше ~30 дней можно чистить периодической задачей.
    """

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[NotificationKind] = mapped_column(
        _enum(NotificationKind, "notification_kind")
    )
    for_date: Mapped[date] = mapped_column(Date)
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus, "notification_status"),
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )
    error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "user_id", "kind", "for_date", name="uq_notification_log_user_kind_date"
        ),
    )