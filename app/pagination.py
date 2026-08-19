from fastapi import Query


def validate_page(page: int = Query(default=1, ge=1, description="Pagina inicial 1")) -> int:
    return page


def validate_page_size(
    page_size: int = Query(default=100, ge=1, le=500, description="Maximo 500")
) -> int:
    return page_size
