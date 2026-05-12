"""shared — utilities consumed by both Shark and Wheel subsystems.

The two subsystems share an Alpaca paper account but must NOT manage each
other's positions. Helpers in this package (e.g. subsystem_ownership)
draw the boundary explicitly so a position opened by one subsystem can
never be modified or closed by the other.
"""

__all__ = ["subsystem_ownership"]
