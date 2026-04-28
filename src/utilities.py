import logging
import os
import pathlib
import subprocess
import sys
import requests
from bs4 import BeautifulSoup

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

def open_daz_product(args):
    """ Opens a specified product in DAZ Studio's Content Library pane."""
    product_name = args.product if args and args.product else None

    if product_name is not None:
        logger.info(f"Opening product '{product_name}' in DAZ Studio...")
        return run_daz_script("OpenProductInContentLibrarySA.dsa", [args.product])
    else:
        logger.error("No product name specified to open.")
        return False

def run_daz_script(script_name: str, script_args:list) -> bool:
    """
    Executes a DAZ Studio script using the DAZ command-line interface.

    Args:
        script_path (str): The file path to the DAZ script to be executed.

    Returns:
        bool: True if the script executed successfully, False otherwise.
    """
    try:
        # Find the DAZ Studio executable
        daz_root = os.getenv("DAZ_STUDIO_EXE_PATH")
        if not daz_root or not os.path.exists(daz_root):
            logger.error("DAZ_STUDIO_EXE_PATH is not set correctly in the environment")
            return False

        script_file = pathlib.Path(__file__).parent.resolve() / script_name
        if not script_file.exists():
            logger.error(f"DAZ script '{script_file}' not found.")
            return False

        command_list = [daz_root]
        for arg in script_args:
            command_list.extend(["-scriptArg", arg])
        command_list.append(str(script_file))

        subprocess.Popen(
            command_list,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        return True
    
    except Exception as e:
        logger.exception(f"Unexpected error executing DAZ script: {e}")
        return False
    
def fetch_json_from_url(url: str, timeout: int = 10) -> dict | None:
    """
    Fetches content from a URL, parses it as JSON, and returns it.

    This function includes robust error handling for common network and data issues.

    Args:
        url (str): The URL to fetch data from.
        timeout (int): The number of seconds to wait for a server response.

    Returns:
        dict | None: A dictionary containing the parsed JSON data if successful,
                      otherwise None.
    """
    logger.debug(f"Fetching JSON from: {url}")
    try:
        # 1. Make the HTTP GET request with a timeout.
        response = requests.get(url, timeout=timeout)

        # 2. Check for HTTP errors (e.g., 404 Not Found, 500 Server Error).
        # This will raise an HTTPError if the status code is 4xx or 5xx.
        response.raise_for_status()

        # 3. Try to parse the response content as JSON.
        # This will raise a JSONDecodeError if the content is not valid JSON.
        return response.json()

    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error fetching {url}: {http_err.response.status_code} {http_err}")
        return None

    except requests.exceptions.JSONDecodeError:
        logger.error(f"Response from {url} is not valid JSON.")
        return None

    except requests.exceptions.RequestException as req_err:
        logger.error(f"Network error fetching {url}: {req_err}")
        return None

    except Exception as e:
        logger.exception(f"Unexpected error fetching {url}: {e}")
        return None


def extract_meta_tags(text) -> dict:
    """ Extracts meta tags from HTML text and returns them as a dictionary.
    
    Args:
        text (str): The HTML text as a string.

    Returns:
        dict: A dictionary of meta tag attributes and their values.
    """

    soup = BeautifulSoup(text,'lxml')

    meta_tags = soup.find_all('meta')

    tag_attributes = {}
   
    for tag in meta_tags:
        for attr in tag.attrs:
            if attr not in ["name", "content", "itemprop", "property","value","http-equiv"]:
                tag_attributes[attr] = tag.attrs[attr]
            elif attr not in["content","value"]:
                content = tag.get_attribute_list("content")[0]
                tag_attributes[tag.attrs[attr]] = content if content else tag.get_attribute_list("value")[0]

    return tag_attributes            

def fetch_html_content(url:str) -> tuple:
    """ Fetches HTML content from a URL and extracts meta tags.

    Args:
        url (str): The URL to fetch HTML content from.

    Returns:
        tuple: A tuple containing the HTML content as a string and a dictionary of meta tag attributes.
    """

    
    html_content = None
    tag_attributes = None
    try:
        response = requests.get(url, timeout=10)
        
        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            # Get the HTML content of the page
            html_content = response.text
            tag_attributes = extract_meta_tags(html_content)
            #print("HTML content fetched successfully!")
            #print(html_content)  # Print the HTML content (optional)
        else:
            logger.warning(f"Failed to fetch {url}: status {response.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching {url}: {e}")

    return html_content, tag_attributes


if __name__ == '__main__':
   pass