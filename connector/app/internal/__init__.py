"""Connector 內網 API —— 只給 CRM 呼叫（設計案 §4.2 / MIGRATION_PLAN §4）。

與 CRM 共用一把 `X-Internal-Token`；部署時只綁 127.0.0.1 或容器內網。
"""
