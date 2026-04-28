# src/inspect_chroma.py

import os
import sys
import chromadb

from managers.managers import chroma_db_manager

def document_exists(doc_id:str) -> bool:
    """ Checks if a document with the given ID exists in the ChromaDB collection.

    Args:
        doc_id (str): The ID of the document to check.

    Returns:
        bool: True if the document exists, False otherwise.
    """
    try:
        # Query the collection by the document ID
        results = chroma_db_manager.collection.get(ids=[doc_id])
        # If the document exists, the results will contain the document
        return len(results["ids"]) > 0
    except Exception as e:
        print(f"Error checking document existence: {e}")
        return False
    
def inspect_collection_metadata(limit: int = 5):
    """ Connects to ChromaDB, fetches a few items from the collection, and prints the data types of their metadata fields.

    Args:
        limit (int): Number of items to fetch and inspect. Default is 5.
    """

    total_docs = chroma_db_manager.collection.count()
    if total_docs == 0:
        print("Collection is empty. Nothing to inspect.")
        return

    print(f"Collection has {total_docs} documents. Fetching {limit} for inspection...")

    # --- Fetch a few items from the collection ---
    # We include the 'metadatas' to inspect them.
    results = chroma_db_manager.collection.get(limit=limit, include=["metadatas"])

    if not results or not results["ids"]:
        print("Could not retrieve any items from the collection.")
        return

    # --- Iterate and print the metadata and its type ---
    for i, item_id in enumerate(results["ids"]):
        metadata = results["metadatas"][i]

        print("\n" + "=" * 50)
        print(f"INSPECTING ITEM ID: {item_id}")
        print("=" * 50)

        for key, value in metadata.items():
            # Get the Python type of the value
            value_type = type(value).__name__

            # Print the key, its type, and a sample of the value
            print(f"  - Key: '{key}'")
            print(f"    Type: {value_type}")
            # Truncate long values for readability
            value_sample = str(value)
            if len(value_sample) > 100:
                value_sample = value_sample[:100] + "..."
            print(f"    Value: {value_sample}")

    print("\n--- Inspection Complete ---")


if __name__ == "__main__":
    #inspect_collection_metadata()
    print (f"DOC CHECK on {sys.argv[1]} -> {document_exists(sys.argv[1])}")
