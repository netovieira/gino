"""Medição do filtro de e-mails no vivo (FASE2-TASK5).

O que o teste vigia:
  * measure_filter() roda uma varredura e devolve métricas por conta
  * Métricas: recebidos, filtrados, notificados, tempo de processamento
  * filter_metrics() devolve o snapshot atual dos contadores
  * Os contadores são registrados corretamente para cada ação do filtro
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import email as mail
from tests.test_email import FakeIMAP, _header, vault  # noqa: F401


def _connect(monkeypatch, fake):
    monkeypatch.setattr(mail, "_imap_open", lambda _a: fake)


def test_measure_filter_returns_metrics(vault, monkeypatch):
    """measure_filter() devolve métricas por conta após uma varredura."""
    mail.add_account("gmail", "eu@gmail.com", notify=True, filters=[
        {"from": "banco", "action": "avisa"},
        {"subject": "promo", "action": "ignora"},
    ])
    fake = FakeIMAP({
        b"1": _header("Extrato", sender="Banco <b@banco.com>",
                      message_id="<m1>"),
        b"2": _header("promo imperdível", sender="x@x.com",
                      message_id="<m2>"),
        b"3": _header("assunto qualquer", sender="y@y.com",
                      message_id="<m3>"),
    })
    _connect(monkeypatch, fake)
    metrics = mail.measure_filter()
    assert "gmail" in metrics
    data = metrics["gmail"]
    # 3 emails recebidos, 1 notificado (banco), 2 filtrados (promo + fora-do-filtro)
    assert data["received"] == 3
    assert data["notified"] == 1
    assert data["filtered"] == 2
    assert data["processing_ms"] >= 0
    assert data["count"] >= 1


def test_filter_metrics_snapshot(vault, monkeypatch):
    """filter_metrics() devolve o snapshot atual dos contadores."""
    mail.add_account("gmail", "eu@gmail.com", notify=True, filters=[
        {"from": "banco", "action": "avisa"},
    ])
    fake = FakeIMAP({
        b"1": _header("Extrato", sender="Banco <b@banco.com>",
                      message_id="<s1>"),
    })
    _connect(monkeypatch, fake)
    mail._filter_metrics.clear()
    mail.poll_once()
    metrics = mail.filter_metrics()
    assert "gmail" in metrics
    assert metrics["gmail"]["received"] == 1
    assert metrics["gmail"]["notified"] == 1


def test_metrics_record_processing_time(vault, monkeypatch):
    """Cada e-mail registra o tempo de processamento."""
    mail.add_account("gmail", "eu@gmail.com", notify=True, filters=[
        {"from": "banco", "action": "avisa"},
    ])
    fake = FakeIMAP({
        b"1": _header("Extrato", sender="Banco <b@banco.com>",
                      message_id="<t1>"),
    })
    _connect(monkeypatch, fake)
    mail._filter_metrics.clear()
    mail.poll_once()
    metrics = mail.filter_metrics()
    assert metrics["gmail"]["processing_ms"] >= 0


def test_metrics_without_filters(vault, monkeypatch):
    """Conta sem filtros: silêncio total (comportamento antigo preservado)."""
    mail.add_account("gmail", "eu@gmail.com", notify=True)
    fake = FakeIMAP({
        b"1": _header("Extrato", sender="Banco <b@banco.com>",
                      message_id="<n1>"),
    })
    _connect(monkeypatch, fake)
    mail._filter_metrics.clear()
    seen: list[dict] = []
    mail.poll_once(hook=seen.append)
    metrics = mail.filter_metrics()
    # Sem filtros, o comportamento antigo: silêncio total (nada notificado)
    assert len(seen) == 0
    # Métricas vazias porque sem filtros não há decisão de filtro
    assert "gmail" not in metrics or metrics.get("gmail", {}).get("received", 0) == 0
