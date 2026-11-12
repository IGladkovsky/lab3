"""Стартовый код сервиса тикетов техподдержки.

Часть классов и функций уже реализована, часть содержит TODO.
Структура данных тикета и правила перехода статуса заданы ниже.
Логику репозитория, сервиса и части эндпоинтов нужно доделать.
"""

from datetime import datetime
from enum import Enum
from functools import lru_cache

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# --- Конфиг ---------------------------------------------------------------

class Settings(BaseModel):
    default_page_size: int = 5
    max_page_size: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()


# --- Исключения -------------------------------------------------------------

class HelpDeskError(Exception):
    """Базовое исключение сервиса."""


class TicketNotFoundError(HelpDeskError):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(f"Тикет {ticket_id} не найден")


class InvalidStatusTransitionError(HelpDeskError):
    def __init__(self, current: "TicketStatus", target: "TicketStatus") -> None:
        self.current = current
        self.target = target
        super().__init__(f"Переход {current} -> {target} запрещён")


# --- Схемы ------------------------------------------------------------------

class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class TicketStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


ALLOWED_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.open: {TicketStatus.in_progress},
    TicketStatus.in_progress: {TicketStatus.resolved, TicketStatus.open},
    TicketStatus.resolved: {TicketStatus.closed, TicketStatus.in_progress},
    TicketStatus.closed: set(),
}


class TicketCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str | None = None
    priority: Priority = Priority.normal
    assignee: str | None = None


class TicketUpdate(BaseModel):
    description: str | None = None
    priority: Priority | None = None
    assignee: str | None = None
    status: TicketStatus | None = None


class Ticket(BaseModel):
    id: int
    title: str
    description: str | None
    priority: Priority
    status: TicketStatus
    assignee: str | None
    created_at: datetime


class TicketPage(BaseModel):
    items: list[Ticket]
    total: int
    page: int
    page_size: int


# --- Репозиторий --------------------------------------------------------------

class TicketRepository:
    """Хранит тикеты в памяти процесса."""

    def __init__(self) -> None:
        self._tickets: dict[int, Ticket] = {}
        self._next_id = 1

    def add(self, ticket: Ticket) -> Ticket:
        self._tickets[ticket.id] = ticket
        return ticket

    def get(self, ticket_id: int) -> Ticket:
        if ticket_id not in self._tickets:
            raise TicketNotFoundError(ticket_id)
        return self._tickets[ticket_id]

    def list_all(self) -> list[Ticket]:
        return list(self._tickets.values())

    def update(self, ticket: Ticket) -> Ticket:
        if ticket.id not in self._tickets:
            raise TicketNotFoundError(ticket.id)
        self._tickets[ticket.id] = ticket
        return self._tickets[ticket.id]


    def delete(self, ticket_id: int) -> None:
        if ticket_id not in self._tickets:
            raise TicketNotFoundError(ticket_id)
        del self._tickets[ticket_id]
        return

    def next_id(self) -> int:
        ticket_id = self._next_id
        self._next_id += 1
        return ticket_id


# --- Сервис ------------------------------------------------------------------

class TicketService:
    def __init__(self, repository: TicketRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def create_ticket(self, payload: TicketCreate) -> Ticket:
        return self._repository.add(Ticket(**payload.model_dump(),
                                      id=self._repository.next_id(),
                                      status=TicketStatus.open,
                                      created_at=datetime.now().replace(microsecond=0)))


    def get_ticket(self, ticket_id: int) -> Ticket:
        return self._repository.get(ticket_id)

    def list_tickets(
        self,
        sort_by: str,
        order: str,
        page: int,
        page_size: int | None,
    ) -> TicketPage:
        if sort_by == "priority":
            priorities_order = list(Priority)
            key_func = lambda t: priorities_order.index(t.priority)
        else:
            key_func = lambda t: getattr(t, sort_by)
        tickets = sorted(self._repository.list_all(),
                         key=key_func,
                         reverse=(order == "desc"))
        page_size = page_size or self._settings.default_page_size
        page_size = (page_size if page_size<self._settings.max_page_size else self._settings.max_page_size)
        return TicketPage(items=tickets[(page-1) * page_size:page * page_size],total=len(tickets),page=page,page_size=page_size)

    def update_ticket(self, ticket_id: int, changes: TicketUpdate) -> Ticket:
        ticket = self._repository.get(ticket_id)
        update_data = {
            k: v for k, v in changes.model_dump(exclude_unset=True).items()
            if v is not None
        }
        ticket_changes = ticket.model_copy(update=update_data)
        if changes.status is not None and changes.status != ticket.status:
            if changes.status not in ALLOWED_TRANSITIONS[ticket.status]:
                raise InvalidStatusTransitionError(ticket.status, changes.status)
        return self._repository.update(ticket_changes)

    def delete_ticket(self, ticket_id: int) -> None:
        self._repository.delete(ticket_id)
        return


# --- Зависимости --------------------------------------------------------------

_repository = TicketRepository()


def get_repository() -> TicketRepository:
    return _repository


def get_service(
    repository: TicketRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> TicketService:
    return TicketService(repository, settings)


# --- Приложение ---------------------------------------------------------------

app = FastAPI(title="HelpDesk Lite")


@app.exception_handler(TicketNotFoundError)
def handle_not_found(request: Request, exc: TicketNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(InvalidStatusTransitionError)
def handle_invalid_transition(request: Request, exc: InvalidStatusTransitionError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.post(
    "/tickets",
    response_model=Ticket,
    status_code=status.HTTP_201_CREATED,
    tags=["tickets"],
    summary="Создать тикет",
)
def create_ticket(payload: TicketCreate, service: TicketService = Depends(get_service)):
    return service.create_ticket(payload)


@app.get(
    "/tickets",
    response_model=TicketPage,
    tags=["tickets"],
    summary="Получить список тикетов",
)
def list_tickets(
    sort_by: str = Query("created_at", pattern="^(created_at|priority)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    service: TicketService = Depends(get_service),
):
    return service.list_tickets(sort_by, order, page, page_size)


@app.get(
    "/tickets/{ticket_id}",
    response_model=Ticket,
    tags=["tickets"],
    summary="Получить тикет по id",
)
def get_ticket(ticket_id: int, service: TicketService = Depends(get_service)):
    return service.get_ticket(ticket_id)


@app.patch(
    "/tickets/{ticket_id}",
    response_model=Ticket,
    tags=["tickets"],
    summary="Изменить часть данных тикета",
)
def update_ticket(
    ticket_id: int,
    changes: TicketUpdate,
    service: TicketService = Depends(get_service),
):
    return service.update_ticket(ticket_id, changes)


@app.delete(
    "/tickets/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tickets"],
    summary="Удалить тикет",
)
def delete_ticket(ticket_id: int, service: TicketService = Depends(get_service)):
    service.delete_ticket(ticket_id)