"""Юнит-экономика по артикулам из отчёта о реализации WB.

Формула прибыли по артикулу:
    К перечислению (продажи − возвраты)
  − логистика (delivery_rub + rebill_logistic_cost)
  − хранение − штрафы − платная приёмка − прочие удержания
  + доплаты (additional_payment)
  − реклама (из статистики кампаний)
  − себестоимость × (продано − возвращено)
  − налог (% от выручки)

Расходы, которые WB не привязал к артикулу (nm_id = 0), считаются общими
расходами кабинета: они вычитаются из итоговой прибыли, но не из прибыли
отдельных артикулов.
"""
from __future__ import annotations

from dataclasses import dataclass, field

AD_MARKERS = ("продвижен", "реклам")


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_ad_deduction(row: dict) -> bool:
    text = f"{row.get('bonus_type_name') or ''} {row.get('supplier_oper_name') or ''}".lower()
    return any(m in text for m in AD_MARKERS)


@dataclass
class Article:
    nm_id: int
    vendor_code: str = ""
    title: str = ""
    sold: int = 0
    returned: int = 0
    revenue: float = 0.0        # выручка по розничной цене (retail_amount)
    to_pay: float = 0.0         # к перечислению за товар
    logistics: float = 0.0
    storage: float = 0.0
    penalties: float = 0.0
    acceptance: float = 0.0
    deductions: float = 0.0
    extra_payments: float = 0.0
    ads: float = 0.0
    cost_price: float | None = None
    tax: float = 0.0

    @property
    def net_qty(self) -> int:
        return self.sold - self.returned

    @property
    def wb_fee(self) -> float:
        return self.revenue - self.to_pay

    @property
    def cogs(self) -> float:
        return (self.cost_price or 0.0) * max(self.net_qty, 0)

    @property
    def profit(self) -> float:
        return (
            self.to_pay
            - self.logistics
            - self.storage
            - self.penalties
            - self.acceptance
            - self.deductions
            + self.extra_payments
            - self.ads
            - self.cogs
            - self.tax
        )

    @property
    def margin(self) -> float | None:
        return self.profit / self.revenue if self.revenue > 0 else None

    @property
    def drr(self) -> float | None:
        return self.ads / self.revenue if self.revenue > 0 else None

    @property
    def roi(self) -> float | None:
        return self.profit / self.cogs if self.cogs > 0 else None

    @property
    def profit_per_unit(self) -> float | None:
        return self.profit / self.net_qty if self.net_qty > 0 else None


@dataclass
class Summary:
    articles: dict[int, Article] = field(default_factory=dict)
    shared_costs: float = 0.0          # расходы без привязки к артикулу
    shared_ad_deductions: float = 0.0  # списания за рекламу в отчёте (не учитываются, если есть статистика рекламы)
    ads_unmatched: float = 0.0         # реклама по артикулам, которых нет в отчёте
    tax_percent: float | None = None
    ads_source: str = "none"           # "stats" | "report" | "none"

    @property
    def revenue(self) -> float:
        return sum(a.revenue for a in self.articles.values())

    @property
    def profit(self) -> float:
        return sum(a.profit for a in self.articles.values()) - self.shared_costs - self.ads_unmatched

    @property
    def ads(self) -> float:
        return sum(a.ads for a in self.articles.values()) + self.ads_unmatched

    @property
    def missing_cost(self) -> list[Article]:
        return [a for a in self.articles.values() if a.cost_price is None and a.net_qty > 0]


def calculate(
    rows: list[dict],
    *,
    costs: dict[int, float],
    ad_spend: dict[int, float] | None,
    tax_percent: float | None,
) -> Summary:
    s = Summary(tax_percent=tax_percent)
    s.ads_source = "stats" if ad_spend is not None else "report"

    for row in rows:
        nm_id = int(row.get("nm_id") or 0)
        doc = (row.get("doc_type_name") or "").strip().lower()
        oper = (row.get("supplier_oper_name") or "").strip().lower()
        sign = -1.0 if doc.startswith("возврат") else 1.0 if doc.startswith("продаж") else 0.0

        costs_here = (
            _f(row, "delivery_rub") + _f(row, "rebill_logistic_cost"),
            _f(row, "storage_fee"),
            _f(row, "penalty"),
            _f(row, "acceptance"),
        )
        deduction = _f(row, "deduction")
        is_ad = deduction and _is_ad_deduction(row)

        if not nm_id:
            if is_ad:
                if ad_spend is None:
                    s.shared_costs += deduction
                else:
                    s.shared_ad_deductions += deduction
            else:
                s.shared_costs += deduction
            s.shared_costs += sum(costs_here) - _f(row, "additional_payment")
            if sign:
                s.shared_costs -= sign * _f(row, "ppvz_for_pay")
            continue

        a = s.articles.get(nm_id)
        if a is None:
            a = s.articles[nm_id] = Article(nm_id=nm_id)
        a.vendor_code = a.vendor_code or str(row.get("sa_name") or "")
        a.title = a.title or str(row.get("subject_name") or "")

        if sign:
            a.revenue += sign * _f(row, "retail_amount")
            a.to_pay += sign * _f(row, "ppvz_for_pay")
            qty = int(_f(row, "quantity"))
            if oper.startswith("продаж") or (sign > 0 and not oper):
                a.sold += qty
            elif oper.startswith("возврат") or (sign < 0 and not oper):
                a.returned += qty

        a.logistics += costs_here[0]
        a.storage += costs_here[1]
        a.penalties += costs_here[2]
        a.acceptance += costs_here[3]
        a.extra_payments += _f(row, "additional_payment")
        if is_ad and ad_spend is not None:
            s.shared_ad_deductions += deduction  # не считаем дважды
        elif is_ad:
            a.ads += deduction
        else:
            a.deductions += deduction

    if ad_spend is not None:
        for nm_id, spend in ad_spend.items():
            if nm_id in s.articles:
                s.articles[nm_id].ads += spend
            else:
                s.ads_unmatched += spend

    for nm_id, a in s.articles.items():
        if nm_id in costs:
            a.cost_price = costs[nm_id]
        if tax_percent:
            a.tax = max(a.revenue, 0.0) * tax_percent / 100.0
    return s
