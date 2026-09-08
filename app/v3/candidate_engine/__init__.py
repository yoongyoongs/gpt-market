"""V3 候选生成引擎（candidate_engine）。

分层：L0 Universe -> L1 Safety -> L2 Multi-Recall -> L3 Enrichment
-> L4 Pareto -> L5 Machine Rank -> L6 Deep Rank -> L7 AI Review -> Final Top30。

新引擎与旧 V1/V2 扫描器（app/services/scanner.py）并行保留，
通过 CandidateEngineConfig.mode（old/new/dual）切换，见任务书 §2。
"""
