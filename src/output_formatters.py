# src/output_formatters.py

import json
from rich.console import Console
from rich.table import Table

def print_pretty(response: dict):
    """Prints the search results in a human-readable, detailed format."""
    if not response or not response.get('results'):
        print("\n--- No results found for your query. ---")
        return

    total_hits = response.get('total_hits', 0)
    returned_count = len(response['results'])
    offset = response.get('offset', 0)
    
    print("\n" + "="*60)
    print(f"Showing results {offset + 1} - {offset + returned_count} of {total_hits} total matches.")
    print("="*60)

    for i, res in enumerate(response['results']):
        metadata = res.get('metadata', {})
        distance = res.get('distance', -1.0)
        
        print(f"\n--- Result #{offset + i + 1} ---")
        print(f"  Name:     {metadata.get('name', 'N/A')}")
        print(f"  Artist:   {metadata.get('artist', 'N/A')}")
        print(f"  Category: {metadata.get('category', 'N/A')}")
        print(f"  SKU:      {res.get('id', 'N/A')}")
        print(f"  URL:      {metadata.get('url', 'N/A')}")
        print(f"  Score (Distance): {distance:.4f} (lower is better)")
        
        if tags := metadata.get('tags'):
            # This handles both comma-delimited strings and JSON strings
            processed_tags = tags.strip(',').replace(',', ', ')
            print(f"  Tags:     {processed_tags}")

    print("\n" + "="*60)


def print_json(response: dict):
    """Prints the full search result as a JSON string."""
    if not response:
        print(json.dumps({"error": "No response"}, indent=2))
        return
    print(json.dumps(response, indent=2))


def print_table(response: dict):
    """Prints the search results in a compact ASCII table."""
    if not response or not response.get('results'):
        print("\n--- No results found for your query. ---")
        return

    console = Console()
    table = Table(show_header=True, header_style="bold magenta")
    
    # Define table columns
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="cyan", no_wrap=True, min_width=20, max_width=40)
    table.add_column("Artist", style="green", min_width=15, max_width=30)
    table.add_column("Category", style="yellow", width=15)
    table.add_column("Score", justify="right", style="bold red", width=8)
    table.add_column("SKU", justify="left", width=10)

    offset = response.get('offset', 0)
    for i, res in enumerate(response['results']):
        metadata = res.get('metadata', {})
        table.add_row(
            str(offset + i + 1),
            metadata.get('name', 'N/A'),
            metadata.get('artist', 'N/A'),
            metadata.get('category', 'N/A'),
            f"{res.get('distance', -1.0):.4f}",
            res.get('id', 'N/A')
        )
        
    console.print(table)
    total_hits = response.get('total_hits', 0)
    print(f"Showing {len(response['results'])} of {total_hits} total matches.")