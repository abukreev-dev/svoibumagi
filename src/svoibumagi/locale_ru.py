"""Russian labels for calculation diagnostics shown to the owner.

Internal reason codes remain stable for exports and tests; the interface never
exposes their English wording.
"""

import re


REASONS = {
    "no committed reports": "Нет проведённых отчётов.",
    "no accounts": "Счета ещё не добавлены.",
    "at least one account has incomplete accounting": "Хотя бы по одному счёту не подтверждена полная история, сверка или конечная оценка.",
    "account valuation dates differ": "Последние оценки счетов относятся к разным датам.",
    "at least one account has an unavailable individual calculation": "По одному из счетов расчёт пока недоступен.",
    "full history from account opening has not been confirmed": "Не подтверждено, что загружены отчёты с открытия счёта.",
    "first report has no confirmed zero opening cash balance": "На начало первого отчёта уже были деньги или нулевой остаток не подтверждён.",
    "first report has opening positions without cost basis": "На начало первого отчёта уже были бумаги; цена их покупки неизвестна.",
    "at least one report has unresolved rows or reconciliation": "В одном из отчётов остались неразобранные строки или неподтверждённая сверка.",
    "instrument absent from report position snapshot": "Бумага отсутствует в итоговой таблице позиций отчёта; нулевой остаток не подтверждён.",
    "ledger cash movements do not match latest reported balance": "Движения денег в журнале не совпадают с конечным остатком брокера.",
    "committed reports disagree on the source account number": "В проведённых отчётах расходятся номера счёта.",
    "broker event has no preserved matching source row": "Для операции брокера не найдена сохранённая строка исходного отчёта.",
    "manual correction is outside committed report coverage": "Ручная корректировка выходит за период проведённых отчётов.",
    "confirmed history start does not match first report": "Подтверждённое начало истории не совпадает с первым отчётом.",
    "cash snapshots disagree with ledger movements between reports": "Остатки денег между отчётами расходятся с движениями в журнале.",
    "position snapshots disagree with ledger trades between reports": "Остатки бумаг между отчётами расходятся со сделками в журнале.",
    "ledger positions do not match an intermediate reported balance": "Позиции в журнале расходятся с промежуточным отчётом.",
    "ledger cash does not match an intermediate reported balance": "Деньги в журнале расходятся с промежуточным отчётом.",
    "non-RUB cash needs currency conversion": "Для валютных денег нужен подтверждённый курс пересчёта.",
    "non-RUB securities need currency conversion": "Для валютных бумаг нужен подтверждённый курс пересчёта.",
    "latest report has no cash snapshot": "В последнем отчёте нет итогового денежного остатка.",
    "latest report has non-RUB cash without a confirmed conversion": "В последнем отчёте есть валютные деньги без подтверждённого курса.",
    "non-RUB trades need currency conversion": "Для валютных сделок нужен подтверждённый курс пересчёта.",
    "FIFO lot quantities do not match latest report": "Количество бумаг в партиях FIFO не совпадает с последним отчётом.",
    "broker fee is recorded by day without trade allocation; FIFO result is unavailable": "Брокер указал комиссию за день без привязки к сделке; результат FIFO пока недоступен.",
    "dividend withholding has no matching separate tax event": "Для дивиденда не найдена соответствующая строка удержанного налога.",
    "withheld tax event has no matching dividend": "Для удержанного налога не найден соответствующий дивиденд.",
    "non-RUB cash movements need currency conversion": "Для валютных денежных операций нужен подтверждённый курс.",
    "cash transfer direction and counterparty are unclassified": "Направление или второй счёт денежного перевода не определены.",
    "internal transfer has no paired event": "Для внутреннего перевода не найдена встречная операция.",
    "security transfer has no confirmed RUB market price on its date": "Для перевода бумаги нет подтверждённой цены в рублях на дату перевода.",
    "security transfer requires cross-account lot replay": "Перевод бумаг требует восстановления партий по обоим счетам.",
    "XIRR requires dated cash flows of both signs": "Для XIRR нужны датированные денежные потоки обоих направлений.",
    "XIRR needs more than one cash-flow date": "Для XIRR нужны денежные потоки хотя бы за две даты.",
    "XIRR has no unique root in supported domain": "Для этих денежных потоков не найдено единственного значения XIRR.",
    "XIRR root uniqueness cannot be established near a stationary point or domain boundary": "Единственность значения XIRR не подтверждена.",
}

BROKERS = {"sber": "Сбер", "alfa": "Альфа-Инвестиции"}
ACCOUNT_KINDS = {"brokerage": "Брокерский счёт", "iis": "ИИС"}
IMPORT_STATES = {"staged": "Предпросмотр", "committed": "Проведён", "rejected": "Отклонён"}
STATUSES = {"matched": "Сошлось", "unavailable": "Нельзя проверить", "mismatch": "Расхождение",
            "new": "Новая операция", "duplicate": "Повтор", "ambiguous": "Неоднозначно",
            "conflict": "Конфликт", "available": "Доступно"}
EVENT_TYPES = {"trade": "Сделка", "cash": "Движение денег", "security_transfer": "Перевод бумаги"}
CASH_KINDS = {"deposit": "Пополнение", "withdrawal": "Вывод", "buy": "Покупка", "sell": "Продажа",
              "dividend": "Дивиденд", "dividend_net": "Дивиденд после удержания", "coupon": "Купон",
              "fee": "Комиссия", "trade_fee": "Комиссия по сделке", "tax_withheld": "Удержанный налог",
              "principal_return": "Погашение номинала", "principal_action": "Изменение номинала",
              "accrued_interest": "НКД", "internal_in": "Внутренний перевод: приход",
              "internal_out": "Внутренний перевод: расход", "transfer_unclassified": "Неразобранный перевод",
              "amortization": "Амортизация", "redemption": "Погашение", "trade_cash": "Расчёт по сделке"}
CHECK_KINDS = {"cash": "Деньги", "position": "Позиция", "completeness": "Полнота",
               "source_total": "Итог источника"}
PRICE_SOURCES = {"manual": "Ручная цена", "moex": "Московская биржа"}

PREFIXES = {
    "Sale has no confirmed opening lot: ": "Для продажи нет подтверждённой партии покупки: ",
    "missing or stale price on valuation date for ": "Нет актуальной цены на дату оценки для ",
}


def reason_ru(reason: str) -> str:
    if reason in REASONS:
        return REASONS[reason]
    if reason.startswith("нет отчёта за "):
        return "Период без отчёта " + reason.removeprefix("нет отчёта за ") + ". Если операций не было, подтвердите это ниже."
    if reason.startswith("FIFO unavailable: "):
        return "Не удалось рассчитать FIFO: " + reason_ru(reason.removeprefix("FIFO unavailable: "))
    for prefix, translated in PREFIXES.items():
        if reason.startswith(prefix):
            tail = reason.removeprefix(prefix)
            if prefix.startswith("missing"):
                return translated + tail.replace(" positions", " позиций") + "."
            return translated + tail
    if re.match(r"^[A-Za-z]", reason):
        return "Расчёт требует дополнительной проверки."
    return reason
