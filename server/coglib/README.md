# coglib

Shared Python package for COG's projects. Provides base config (Pydantic), database (SQLAlchemy), logging, LLM access, and utilities.

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

## LLM calls

One signature across Anthropic, OpenAI and Google, with correct cost accounting
(each provider reports reasoning tokens differently — see `CLAUDE.md`).

```python
from coglib.llm import call

r = call("opus", "Summarise this", system="Be terse")
r = call("flash", "What is this?", image="photo.jpg")
print(r.text, r.cost, r.secs, r.reasoning_tokens)
```

```bash
python -m coglib.llm --list          # models, rates, which providers have keys
python -m coglib.llm haiku "hello"
```

Keys live in `~/.config/comar/{anthropic,openai,gemini}_api_key`, or the
matching `*_API_KEY` env vars.
