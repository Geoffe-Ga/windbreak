"""Process A: the main research pipeline (SPEC S5.1).

Hosts the Market Connector, Screener, Forecast Engine, and Trade Selector.
Per SPEC S5.1 this process runs with **no trade credentials** -- it may hold
only public/read-only market access plus LLM and web-research keys, and it
emits normalized order intents that the Risk Kernel must approve before any
capital moves.
"""
