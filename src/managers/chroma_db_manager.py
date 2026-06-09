""" Manages interactions with ChromaDB for vector storage and retrieval. """

import chromadb
import logging
from typing import List, Optional
from embedding_utils import generate_embeddings

logger = logging.getLogger(__name__)

def build_where_clause(
    tags: Optional[List[str]] = None,
    artists: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    compatible_figures: Optional[List[str]] = None,
):
    """
    Dynamically builds a ChromaDB 'where' filter for multiple fields.
    Uses $and for criteria across different fields (e.g., category AND artist)
    and $or for criteria within the same field (e.g., category is Clothing OR Hair).
    """

    # Conditions that must ALL be met are added to this list
    if not any([tags, artists, categories, compatible_figures]):
        return None

    and_conditions = []

    # Helper function to create a filter block for a list of values
    def create_or_condition(field_name: str, values: List[str]):
        if not values:
            return

        # For the 'category' field, which is a single string, we use exact match ($eq)
        if field_name == "category":
            conditions = [{field_name: {"$eq": value}} for value in values]
        # For fields stored as JSON strings of lists, we use substring matching ($contains)
        else:
            conditions = [{field_name: {"$contains": value}} for value in values]

        if len(conditions) > 1:
            and_conditions.append({"$or": conditions})
        elif conditions:
            and_conditions.append(conditions[0])

    # Build conditions for each filter type passed to the function
    if tags:               create_or_condition("tags", tags)
    if artists:            create_or_condition("artist", artists)
    if categories:         create_or_condition("category", categories)
    if compatible_figures: create_or_condition("compatible_figures", compatible_figures)

    # Return the final filter structure for the ChromaDB query
    if not and_conditions:
        return None
    if len(and_conditions) == 1:
        return and_conditions[0]

    return {"$and": and_conditions}


class ChromaDbManager:
    """Handles all interactions with ChromaDB."""

    def __init__(self, chroma_db_path:str, collection_name:str):
        """Create a ChromaDB manager instance.

        Args:
            chroma_db_path (str): Path to the ChromaDB database directory.
            collection_name (str): Name of the collection to use within ChromaDB.
        """
        
        self.chroma_db_path = chroma_db_path
        self.collection_name = collection_name
        self.client = chromadb.PersistentClient(path=chroma_db_path)
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},  # Example for 768-dim embeddings
        )

        logger.info(f"ChromaDB opened — path: {self.chroma_db_path!r}, collection: {self.collection_name!r}")
        
        
    def _clean_metadata(self, item: dict) -> dict:
        """ Cleans metadata dictionary by removing None values and non-serializable types.

        Args:
            item (dict): Input metadata dictionary.

        Returns:
            dict: Cleaned metadata dictionary.
        """
        clean = {}
        for key, value in item.items():
            if value is not None and isinstance(value, (str, int, float, bool)):
                clean[key] = value
        return clean
    
    def reconnect(self) -> None:
        """Recreates the ChromaDB client and refreshes the collection reference.

        Call this when an upsert fails with a stale-connection or missing-collection
        error so the next operation gets a fresh handle.
        """
        self.client = chromadb.PersistentClient(path=self.chroma_db_path)
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"ChromaDB reconnected — path: {self.chroma_db_path!r}, collection: {self.collection_name!r}")

    def get_all_ids(self) -> set:
        """Returns the set of all document IDs currently stored in the collection."""
        return set(self.collection.get(include=[])["ids"])

    def reset_collection(self) -> None:
        """Deletes and recreates the default collection, leaving it empty and ready for a fresh index.

        Updates self.collection so existing references stay valid.
        """
        logger.info(f"Resetting ChromaDB collection {self.collection_name!r}...")
        try:
            self.client.delete_collection(name=self.collection_name)
        except Exception:
            logger.warning(f"Collection {self.collection_name!r} did not exist; nothing to delete.")
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(f"Collection {self.collection_name!r} reset and ready.")

    def search(
        self,
        prompt: str,
        tags: Optional[List[str]] = None,
        artists: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        compatible_figures: Optional[List[str]] = None,
        limit: int = 10,
        offset: int = 0,
        max_results: int = 500,
        score_threshold: float = 1.0,
        sort_by: str = "relevance",
        sort_order: str = "descending",
    ):
        """Searches the ChromaDB collection with the given parameters.

        Args:
            prompt (str): The search prompt to generate the query embedding.
            tags (List[str], optional): List of tags to filter by. Defaults to None.
            artists (List[str], optional): List of artists to filter by. Defaults to None.
            categories (List[str], optional): List of categories to filter by. Defaults to None.
            compatible_figures (List[str], optional): List of compatible figures to filter by. Defaults to None.
            limit (int, optional): Number of results to return from the filtered set. Defaults to 10.
                Used by the MCP /query endpoint. The UI /search endpoint uses max_results instead.
            offset (int, optional): Offset into the filtered result set. Defaults to 0.
                Used by the MCP /query endpoint. The UI /search endpoint always passes 0.
            max_results (int, optional): Maximum number of candidates to fetch from ChromaDB.
                This is the fixed pool size — independent of limit/offset — so total_hits is
                stable across requests. Defaults to 500. The UI /search endpoint controls this
                directly; the MCP /query endpoint leaves it at the default.
            score_threshold (float, optional): Maximum cosine distance for a result to be
                included. Defaults to 1.0 (include everything).
            sort_by (str, optional): Field to sort by. Use 'relevance' for cosine-distance order
                (default), or any metadata field name (e.g. 'name', 'artist').
            sort_order (str, optional): 'ascending' or 'descending'. Only applied when
                sort_by is not 'relevance'. Defaults to 'descending'.

        Returns:
            dict: Search results including total_hits, limit, offset, and list of results.
        """

        # --- 1. Generate Query Embedding ---
        query_embedding = generate_embeddings(prompt, is_query=True)

        # --- 2. Build the Combined Metadata Filter ---
        where_filter = build_where_clause(
            tags=tags,
            artists=artists,
            categories=categories,
            compatible_figures=compatible_figures,
        )
        if where_filter:
            logger.debug(f"Applying metadata filter: {where_filter}")

        # Fixed candidate pool — does not grow with offset/limit so total_hits is stable.
        query_limit = max_results

        # --- 3. Query ChromaDB ---

        results = self.collection.query (
            query_embeddings=[query_embedding.tolist()],
            n_results=query_limit,
            where=where_filter,
            include=["metadatas", "distances", "documents"],
        )


        # --- 4. Post-process Results (Filtering by Score) ---
        processed_results = []
        if results["ids"]:
            for i in range(len(results["ids"][0])):
                dist = results["distances"][0][i]
                if dist <= score_threshold:
                    processed_results.append(
                        {
                            "id": results["ids"][0][i],
                            "distance": dist,
                            "relevance_score": round(1.0 - dist, 4),
                            "metadata": results["metadatas"][0][i],
                        }
                    )

        # --- 5. Sorting Logic ---
        reverse_order = sort_order == "descending"
        if sort_by != "relevance":
            # Sort by a metadata field, handling potential missing keys gracefully
            processed_results.sort(
                key=lambda x: x["metadata"].get(sort_by) or "",  # Fallback for sorting
                reverse=reverse_order,
            )
        # Note: ChromaDB already returns results sorted by relevance (distance ascending)

        # --- 6. Apply Pagination and Return ---
        paginated_results = processed_results[offset : offset + limit]

        logger.debug(f"Result set size: {len(paginated_results)}")

        return {
            "total_hits": len(processed_results),
            "limit": limit,
            "offset": offset,
            "results": paginated_results,
        }
        

    def get_db_stats(self):
        """Returns the total document count from the ChromaDB collection.

        Returns:
            dict: A dictionary containing total document count.
        """
        return {"total_docs": self.collection.count()}

