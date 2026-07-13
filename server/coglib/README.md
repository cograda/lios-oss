# coglib

Shared Python package for COG's projects. Provides base config (Pydantic), database (SQLAlchemy), logging, and utilities.

Part of [comar-oss](https://github.com/cograda/comar-oss), MIT licensed — see the repository root [LICENSE](../../LICENSE).

## Install

```bash
pip install -e .
```

## Quick Start

```python
from coglib import CogSettings, Base, Database

# Subclass settings per project
class MySettings(CogSettings):
    app_name: str = "my-project"

# Define models
from sqlalchemy import Column, Integer, String

class Widget(Base):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
    name = Column(String(100))

# Use database
settings = MySettings()
db = Database(url=settings.database.url)
db.create_tables()

with db.session() as session:
    session.add(Widget(name="thing"))
```
