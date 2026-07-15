"""Tests for Proxmox storage content ``size`` parsing in ``ui``."""

from __future__ import annotations


def test_listing_item_size_bytes_int(ui_module):
    assert ui_module._listing_item_size_bytes({"size": 1048576}) == 1048576


def test_listing_item_size_bytes_float(ui_module):
    assert ui_module._listing_item_size_bytes({"size": 1048576.0}) == 1048576


def test_listing_item_size_bytes_string_float(ui_module):
    assert ui_module._listing_item_size_bytes({"size": "10737418240.0"}) == 10737418240


def test_listing_item_size_bytes_missing_or_zero(ui_module):
    assert ui_module._listing_item_size_bytes({}) == 0
    assert ui_module._listing_item_size_bytes({"size": 0}) == 0
    assert ui_module._listing_item_size_bytes({"size": "0"}) == 0
    assert ui_module._listing_item_size_bytes({"size": None}) == 0
