"""CRM 內網 API —— 只給 Connector 呼叫（設計案 §4.1 / MIGRATION_PLAN §4）。

全部走 `X-Internal-Token` 認證，**禁止公網暴露**（部署時只綁 127.0.0.1 或容器內網）。
"""
