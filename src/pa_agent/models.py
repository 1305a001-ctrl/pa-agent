from signals_contract import Signal

# Re-export Signal so existing imports of `from pa_agent.models import Signal` keep working.
__all__ = ["Signal"]
