"""Passwords are held only by the private service, never saved by this client."""
def load(_account): return None
def exists(_account): return False
def clear(_account): return True
def save(*_args, **_kwargs): return False
