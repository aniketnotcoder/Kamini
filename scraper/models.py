from typing import Optional
from pydantic import BaseModel


class ScrapedItem(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    thumbnail: Optional[str] = None


class ScrapeResult(BaseModel):
    source: str
    status: int
    final_url: str
    title: Optional[str]
    framework: Optional[str]
    next_data: bool
    links_found: int
    images_found: int
    scripts_found: int
    items: list[ScrapedItem]
