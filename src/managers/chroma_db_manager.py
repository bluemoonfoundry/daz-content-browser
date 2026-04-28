""" Manages interactions with ChromaDB for vector storage and retrieval. """

import chromadb
import json
import logging
import os
from collections import Counter
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
        score_threshold: float = 1.0,
        sort_by: str = "relevance",
        sort_order: str = "descending",
    ):
        """ Searches the ChromaDB collection with the given parameters.

        Args:
            prompt (str): The search prompt to generate the query embedding.    
            tags (List[str], optional): List of tags to filter by. Defaults to None.
            artists (List[str], optional): List of artists to filter by. Defaults to None.
            categories (List[str], optional): List of categories to filter by. Defaults to None.
            compatible_figures (List[str], optional): List of compatible figures to filter by. Defaults to None.
            limit (int, optional): Number of results to return. Defaults to 10. Max 100.
            offset (int, optional): Offset for pagination. Defaults to 0.           
            score_threshold (float, optional): Maximum distance for a result to be considered relevant. Defaults to 2.0.
            sort_by (str, optional): Field to sort by. Use 'relevance' for cosine-distance order
                (default), or any metadata field name (e.g. 'name', 'artist').
            sort_order (str, optional): 'ascending' or 'descending'. Only applied when
                sort_by is not 'relevance'. Defaults to 'descending'.
        
        Returns:
            dict: Search results including total hits, limit, offset, and list of results.

        Raises:
            
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

        # Fetch a larger number of results to allow for post-filtering, sorting, and pagination
        query_limit = (offset + limit) * 5 + 20  # A generous buffer

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
        

    def load_sqlite_to_chroma(self, valid_products:list) -> bool:
        """
        If rebuild then reads all products from an SQLite database, generates new embeddings,
        and completely rebuilds the ChromaDB collection. Otherwise, only update the Chroma
        database with products that are newer than the checkpoint_date

        Args:
            valid_products (list): List of product dictionaries to be added/updated in ChromaDB

        Returns:
            bool: True if the operation was successful, False otherwise.
        """
                
        texts_to_embed = [p["embedding_text"] for p in valid_products]
        ids_to_upsert = [str(p["sku"]) for p in valid_products]
        metadatas_to_upsert: list[dict[str, str | int | float | bool | None]] = [
            self._clean_metadata(p) for p in valid_products
        ]
        documents_to_upsert = [p["embedding_text"] for p in valid_products]

        # --- 4. Generate All Embeddings in a Single Batch ---
        logger.info(f"Generating embeddings for {len(texts_to_embed)} documents...")
        embedding_list = generate_embeddings(texts_to_embed, is_query=False).tolist()
        logger.info("Embeddings generated successfully.")

        # --- 5. Upsert the Batch into ChromaDB ---
        try:
            self.collection.upsert(
                ids=ids_to_upsert,
                embeddings=embedding_list,
                documents=documents_to_upsert,
                metadatas=metadatas_to_upsert,
            )
            logger.info(f"Upserted {len(ids_to_upsert)} documents into collection '{self.collection_name}'.")
        except Exception as e:
            logger.error(f"Error publishing to ChromaDB: {e}")
            return False

        return True
    
    def get_db_stats(self):
        """Gathers and returns statistics and histograms for all key filterable fields.

        Returns:
            dict: A dictionary containing total document count, last update date, and histograms for tags, artists, compatible figures, and categories. 
        """

        total_docs = self.collection.count()
        if total_docs == 0:
            return {"total_docs": 0, "last_update": "N/A", "histograms": {}}

        all_metadatas = self.collection.get(include=["metadatas"])["metadatas"]

        # Initialize Counters for Histograms
        tag_counter, artist_counter, figure_counter, category_counter = (
            Counter(),
            Counter(),
            Counter(),
            Counter(),
        )

        # Find the Last Update Date
        last_update_dates = [
            meta.get("last_updated") for meta in all_metadatas if meta.get("last_updated")
        ]
        last_update = max(last_update_dates) if last_update_dates else "N/A"

        # Iterate and Process All Metadata
        for meta in all_metadatas:
            if category := meta.get("category"):
                category_counter.update([category])

            def parse_and_update_counter(field_name: str, counter: Counter):
                value = meta.get(field_name)
                if value:
                    try:
                        if isinstance(value, list):
                            item_list = value
                        else:
                            item_list = str(value).split(",")
                        counter.update(x.strip() for x in item_list if x.strip())
                    except TypeError:
                        pass  # Ignore malformed data

            parse_and_update_counter("tags", tag_counter)
            parse_and_update_counter("artist", artist_counter)
            parse_and_update_counter("compatible_figures", figure_counter)


        # We need to reduce the tag list because it may be very long when it contains small counts
        threshold = int(os.getenv("STATS_TAG_THRESHOLD", "10"))
        filtered_dict = {
            item: count 
            for item, count in tag_counter.items() 
            if count >= threshold
        }
        
        tag_counter = Counter(filtered_dict)
        
        return { 
            "total_docs": total_docs,
            "last_update": last_update,
            "histograms": {
                "tags": tag_counter,
                "artists": artist_counter,
                "compatible_figures": figure_counter,
                "categories": category_counter,
            },
        }

