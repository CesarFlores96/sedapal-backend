from typing import List
from fastembed import TextEmbedding

_model = None

def get_embedding_model() -> TextEmbedding:
    """Lazy initialize the fastembed TextEmbedding model (BAAI/bge-small-en-v1.5, dimension 384)."""
    global _model
    if _model is None:
        # BAAI/bge-small-en-v1.5 is the fastembed default and has exactly 384 dimensions.
        _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _model

def get_embedding(text: str) -> List[float]:
    """Generates a 384-dimensional vector embedding for the given text."""
    clean_text = text.strip()
    if not clean_text:
        return [0.0] * 384
    
    model = get_embedding_model()
    # model.embed returns a generator, convert it to list
    embeddings = list(model.embed([clean_text]))
    return [float(x) for x in embeddings[0]]
