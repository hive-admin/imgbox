import sys
import subprocess
import importlib.util
import os
import re
import uuid
import time
import csv
import glob
import json
import signal
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ==============================================================================
#                        DEPENDENCY MANAGEMENT
# ==============================================================================

def check_and_install_dependencies():
    """
    Checks for required third-party packages and installs them automatically
    if they are missing from the current environment.
    """
    required_packages = {
        'curl_cffi': 'curl_cffi', 
        'colorama': 'colorama'
    }
    
    missing_packages = []
    
    for package, import_name in required_packages.items():
        if importlib.util.find_spec(import_name) is None:
            missing_packages.append(package)
            
    if missing_packages:
        print(f"Missing requirements detected. Installing: {', '.join(missing_packages)}...")
        try:
            subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + missing_packages)
            print("Dependencies installed successfully. Initializing application...\n")
            importlib.invalidate_caches()
        except subprocess.CalledProcessError as e:
            print(f"Error: Failed to install dependencies automatically. Please install {missing_packages} manually.")
            sys.exit(1)
        except Exception as e:
            print(f"Unexpected error during dependency installation: {e}")
            sys.exit(1)

# Ensure dependencies are present before importing
check_and_install_dependencies()

from curl_cffi import requests
from colorama import init, Fore, Style

# Initialize Colorama for cross-platform colored terminal output
init(autoreset=True)

# ==============================================================================
#                               CONFIGURATION
# ==============================================================================

# --- Input / Output Settings ---
# Set to 'set_file_path' to force interactive user prompt, or provide a raw string path.
IMAGE_DIRECTORY = 'set_file_path'

# Dynamic CSV Filename: batch_yyyy_mm_dd_hhmmss.csv
timestamp_str = datetime.now().strftime("%Y_%m_%d_%H%M%S")
CSV_OUTPUT_FILE = f"batch_{timestamp_str}.csv"

STATE_FILE = 'upload_state.json'  # Used for tracking progress to enable resume functionality

# --- Execution Logic Settings ---
BASE_WAIT_TIME = 5   # Initial wait time (seconds) for backoff strategy
MAX_WAIT_TIME = 60   # Cap for the exponential backoff wait time
SESSION_RENEW_LIMIT = 100  # Number of uploads before forcing a session refresh
MAX_RETRIES = 5      # Maximum attempts per file before marking as failed
REQUEST_TIMEOUT = 30 # HTTP request timeout in seconds

# --- Imgbox API Constants ---
# API specific flags: '2' = Adult Content, '100c' = Square Thumbnail
CONTENT_TYPE = '2'
THUMBNAIL_SIZE = '100c'
COMMENTS_ENABLED = '0'
GALLERY_OPTION = 'false'

# --- File Validation Limits ---
HOST_MAX_FILE_SIZE_MB = 10  # Imgbox hard limit
SAFETY_MARGIN = 0.90        # Safety buffer to prevent edge-case rejections
MAX_FILE_SIZE_MB = HOST_MAX_FILE_SIZE_MB * SAFETY_MARGIN
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif'}
MIN_FILE_SIZE_BYTES = 100

# ==============================================================================
#                        GLOBAL STATE & SIGNAL HANDLING
# ==============================================================================

class GlobalState:
    """
    Singleton to manage application state across modules, primarily for
    handling graceful shutdowns during interrupts.
    """
    interrupted = False
    uploader = None

global_state = GlobalState()

def signal_handler(signum, frame):
    """
    Intercepts SIGINT (Ctrl+C). Sets a flag to allow the current operation
    to complete/save before exiting, preventing data corruption.
    """
    if not global_state.interrupted:
        global_state.interrupted = True
        print(f"\n\n{Fore.YELLOW}⚠ Interrupt received. Finishing current operation and saving progress...")
        print(f"{Fore.YELLOW}⚠ Press Ctrl+C again to force quit (may lose data)")
    else:
        print(f"\n{Fore.RED}✖ Force quit. Some data may be lost.")
        sys.exit(1)

# Register the signal handler
signal.signal(signal.SIGINT, signal_handler)

# ==============================================================================
#                               HELPER CLASSES
# ==============================================================================

class Console:
    """
    Utilities for formatted, colored console output and progress visualization.
    """
    
    @staticmethod
    def info(msg, indent=0):
        prefix = "  " * indent
        print(f"{prefix}{Fore.CYAN}ℹ  {msg}")

    @staticmethod
    def success(msg, indent=0):
        prefix = "  " * indent
        print(f"{prefix}{Fore.GREEN}✔  {msg}")

    @staticmethod
    def error(msg, indent=0):
        prefix = "  " * indent
        print(f"{prefix}{Fore.RED}✖  {msg}")

    @staticmethod
    def warning(msg, indent=0):
        prefix = "  " * indent
        print(f"{prefix}{Fore.YELLOW}⚠  {msg}")

    @staticmethod
    def header(msg):
        print(f"\n{Style.BRIGHT}{Fore.MAGENTA}╔{'═'*58}╗")
        print(f"{Style.BRIGHT}{Fore.MAGENTA}║ {msg:<56} ║")
        print(f"{Style.BRIGHT}{Fore.MAGENTA}╚{'═'*58}╝")

    @staticmethod
    def group_start(msg):
        print(f"\n{Fore.CYAN}{Style.BRIGHT}┌─ {msg}")
    
    @staticmethod
    def group_end():
        print(f"{Fore.CYAN}{Style.DIM}└{'─'*60}")

    @staticmethod
    def live_countdown(seconds, message="Waiting", indent=0):
        """
        Displays a live updating countdown bar on the current line.
        Clears the line upon completion.
        """
        prefix = "  " * indent
        max_width = 70
        
        for i in range(seconds, 0, -1):
            if global_state.interrupted:
                sys.stdout.write(f"\r{' ' * max_width}\r")
                sys.stdout.flush()
                return
            
            bar_length = 20
            filled = int((seconds - i + 1) / seconds * bar_length)
            bar = '█' * filled + '░' * (bar_length - filled)
            
            output = f"{prefix}{Fore.YELLOW}⏳ {message}: {bar} {i}s"
            sys.stdout.write(f"\r{output}{' ' * (max_width - len(output))}")
            sys.stdout.flush()
            time.sleep(1)
        
        # Clear line
        sys.stdout.write(f"\r{' ' * max_width}\r")
        sys.stdout.flush()
    
    @staticmethod
    def progress(current, total, msg=""):
        percentage = (current / total * 100) if total > 0 else 0
        bar_length = 30
        filled = int(percentage / 100 * bar_length)
        bar = '█' * filled + '░' * (bar_length - filled)
        
        output = f"{Fore.CYAN}Progress: [{bar}] {percentage:.0f}% ({current}/{total})"
        if msg:
            output += f" - {msg}"
        
        print(output)


class FileValidator:
    """
    Enforces file integrity constraints before attempting upload.
    Checks existence, extension, and file size against host limits.
    """
    
    @staticmethod
    def validate_file(file_path: str) -> Tuple[bool, str]:
        try:
            path = Path(file_path)
            
            if not path.exists():
                return False, "File does not exist"
            
            if not path.is_file():
                return False, "Path is not a file"
            
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                return False, f"Invalid extension: {path.suffix}"
            
            size_bytes = path.stat().st_size
            
            if size_bytes < MIN_FILE_SIZE_BYTES:
                return False, f"File too small ({size_bytes} bytes)"
            
            if size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
                size_mb = size_bytes / (1024 * 1024)
                return False, f"File too large ({size_mb:.2f} MB > {MAX_FILE_SIZE_MB} MB)"
            
            # sanity check: try reading a byte to ensure read permissions
            with open(file_path, 'rb') as f:
                f.read(1)
            
            return True, ""
            
        except PermissionError:
            return False, "Permission denied"
        except Exception as e:
            return False, f"Validation error: {str(e)}"


class StateManager:
    """
    Handles persistence of the upload session (JSON serialization).
    Allows the script to resume from where it left off after an interruption.
    """
    
    @staticmethod
    def load_state() -> Dict:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            Console.warning(f"Could not load state file: {e}")
        return {'completed_files': [], 'results': []}
    
    @staticmethod
    def save_state(completed_files: List[str], results: List[Dict]):
        try:
            state = {
                'completed_files': completed_files,
                'results': results,
                'timestamp': datetime.now().isoformat()
            }
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            Console.warning(f"Could not save state: {e}")
    
    @staticmethod
    def clear_state():
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except Exception as e:
            Console.warning(f"Could not remove state file: {e}")


class ImgboxUploader:
    """
    Core class for Imgbox API interaction.
    Manages HTTP sessions, token generation, multipart encoding, and upload requests.
    Uses curl_cffi to impersonate a browser TLS fingerprint.
    """
    
    def __init__(self):
        self.session = None
        self.csrf_token = None
        self.upload_token_data = None
        self.processed_count = 0
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://imgbox.com/'
        }

    def initialize_session(self) -> bool:
        """
        Establishes a new session by visiting the homepage (to get CSRF)
        and requesting a new upload token from the API.
        """
        try:
            if self.session:
                try:
                    self.session.close()
                except:
                    pass
            
            # impersonate="chrome" prevents 10054 Connection Reset errors
            self.session = requests.Session(impersonate="chrome")
            resp = self.session.get('https://imgbox.com/', timeout=REQUEST_TIMEOUT)
            
            if resp.status_code != 200:
                raise Exception(f"Homepage returned status {resp.status_code}")
            
            # Extract CSRF token required for subsequent POST requests
            csrf_match = re.search(r'<meta content="(.*?)" name="csrf-token" />', resp.text)
            if not csrf_match:
                raise Exception("CSRF Token not found in homepage")
            
            self.csrf_token = csrf_match.group(1)
            self.headers['X-CSRF-Token'] = self.csrf_token
            self.headers['X-Requested-With'] = 'XMLHttpRequest'
            self.headers['Origin'] = 'https://imgbox.com'

            # Request upload session tokens
            token_resp = self.session.post(
                'https://imgbox.com/ajax/token/generate',
                data={
                    'gallery': GALLERY_OPTION,
                    'gallery_title': '',
                    'comments_enabled': COMMENTS_ENABLED
                },
                headers=self.headers,
                timeout=REQUEST_TIMEOUT
            )
            
            if token_resp.status_code != 200:
                raise Exception(f"Token generation returned status {token_resp.status_code}")
            
            self.upload_token_data = token_resp.json()
            
            if not all(k in self.upload_token_data for k in ['token_id', 'token_secret', 'gallery_id', 'gallery_secret']):
                raise Exception("Incomplete token data received")
            
            Console.success(f"Session initialized (Token: {self.upload_token_data['token_id']})", indent=1)
            self.processed_count = 0
            return True
            
        except requests.exceptions.Timeout:
            Console.error("Session initialization timed out", indent=1)
            return False
        except requests.exceptions.ConnectionError as e:
            Console.error(f"Connection error: {e}", indent=1)
            return False
        except Exception as e:
            Console.error(f"Initialization failed: {e}", indent=1)
            return False

    def _encode_multipart(self, fields: Dict, files: Dict, boundary: Optional[str] = None) -> Tuple[bytes, str]:
        """
        Manually constructs the multipart/form-data body.
        Necessary because standard library implementations may not match the specific
        formatting expected by the Imgbox server or curl_cffi wrapper.
        """
        if boundary is None:
            boundary = uuid.uuid4().hex
        
        body = b""
        
        for k, v in fields.items():
            body += b"--" + boundary.encode() + b"\r\n"
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += str(v).encode() + b"\r\n"
        
        for k, (fn, fp, ct) in files.items():
            content = fp.read()
            body += b"--" + boundary.encode() + b"\r\n"
            body += f'Content-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'.encode()
            body += f'Content-Type: {ct}\r\n\r\n'.encode()
            body += content + b"\r\n"
        
        body += b"--" + boundary.encode() + b"--\r\n"
        return body, f"multipart/form-data; boundary={boundary}"

    def _detect_content_type(self, file_path: str) -> str:
        """Determines MIME type based on extension."""
        ext = Path(file_path).suffix.lower()
        mime_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif'
        }
        return mime_types.get(ext, 'image/jpeg')

    def upload_image(self, file_path: str) -> bool:
        """
        Performs the actual file upload to the /upload/process endpoint.
        Handles session validation and multipart encoding.
        """
        filename = os.path.basename(file_path)
        
        # Auto-initialize session if missing
        if not self.session or not self.upload_token_data:
            Console.warning("No valid session, attempting to initialize...")
            if not self.initialize_session():
                return False
        
        fields = {
            'token_id': str(self.upload_token_data['token_id']),
            'token_secret': self.upload_token_data['token_secret'],
            'content_type': CONTENT_TYPE,
            'thumbnail_size': THUMBNAIL_SIZE,
            'gallery_id': self.upload_token_data['gallery_id'],
            'gallery_secret': self.upload_token_data['gallery_secret'],
            'comments_enabled': COMMENTS_ENABLED
        }

        try:
            content_type = self._detect_content_type(file_path)
            
            with open(file_path, 'rb') as f:
                form_files = {'files[]': (filename, f, content_type)}
                body, ctype = self._encode_multipart(fields, form_files)
            
            up_headers = self.headers.copy()
            up_headers['Content-Type'] = ctype
            
            resp = self.session.post(
                'https://imgbox.com/upload/process',
                headers=up_headers,
                data=body,
                timeout=REQUEST_TIMEOUT
            )
            
            return resp.status_code == 200
            
        except requests.exceptions.Timeout:
            Console.warning("Upload request timed out")
            return False
        except requests.exceptions.ConnectionError:
            Console.warning("Connection error during upload")
            return False
        except FileNotFoundError:
            Console.warning(f"File not found: {file_path}")
            return False
        except Exception as e:
            Console.warning(f"Upload exception: {e}")
            return False

    def finalize_and_get_link(self) -> Optional[str]:
        """
        Submits the final form to confirm upload and scrapes the response
        HTML to retrieve the direct link to the image.
        """
        try:
            fin_headers = self.headers.copy()
            # Remove XMLHttpRequest to simulate standard form submission
            if 'X-Requested-With' in fin_headers:
                del fin_headers['X-Requested-With']

            data = {
                'utf8': '✓',
                'authenticity_token': self.csrf_token,
                'token_id': str(self.upload_token_data['token_id']),
                'token_secret': self.upload_token_data['token_secret']
            }
            
            resp = self.session.post(
                'https://imgbox.com/',
                data=data,
                headers=fin_headers,
                timeout=REQUEST_TIMEOUT
            )
            
            if resp.status_code != 200:
                Console.warning(f"Finalization returned status {resp.status_code}")
                return None
            
            # Regex to find valid image URLs
            regex = r'https://[a-zA-Z0-9]+\.imgbox\.com/[a-zA-Z0-9/_]+_o\.(?:jpg|jpeg|png|gif)'
            links = re.findall(regex, resp.text, re.IGNORECASE)
            
            # Return the last link found (corresponds to the most recent upload)
            return links[-1] if links else None
            
        except requests.exceptions.Timeout:
            Console.error("Finalization timed out")
            return None
        except requests.exceptions.ConnectionError:
            Console.error("Connection error during finalization")
            return None
        except Exception as e:
            Console.error(f"Finalization failed: {e}")
            return None

    def cleanup(self):
        """Closes the HTTP session to free resources."""
        if self.session:
            try:
                self.session.close()
            except:
                pass

# ==============================================================================
#                             CORE LOGIC
# ==============================================================================

def exponential_backoff(attempt: int, base_wait: int = BASE_WAIT_TIME) -> int:
    """Calculates wait time for retries using exponential backoff."""
    wait_time = min(base_wait * (2 ** (attempt - 1)), MAX_WAIT_TIME)
    return wait_time


def process_file_robustly(uploader: ImgboxUploader, file_path: str, index: int, total: int) -> Dict:
    """
    Orchestrates the lifecycle of a single file upload.
    Includes validation, session renewal checks, multi-phase retries,
    and error handling.
    """
    filename = os.path.basename(file_path)
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    
    Console.group_start(f"[{index}/{total}] {filename} ({file_size_mb:.2f} MB)")

    # Check for user interruption
    if global_state.interrupted:
        Console.warning("Skipped (user interrupt)", indent=1)
        Console.group_end()
        return {"file": filename, "status": "skipped", "url": "", "error": "User interrupt"}

    # Phase 0: Validation
    is_valid, error_msg = FileValidator.validate_file(file_path)
    if not is_valid:
        Console.error(f"Invalid: {error_msg}", indent=1)
        Console.group_end()
        return {"file": filename, "status": "failed", "url": "", "error": f"Validation: {error_msg}"}

    # Proactive session renewal
    if uploader.processed_count >= SESSION_RENEW_LIMIT:
        Console.info(f"Renewing session (limit: {SESSION_RENEW_LIMIT})...", indent=1)
        if not uploader.initialize_session():
            Console.group_end()
            return {"file": filename, "status": "failed", "url": "", "error": "Session renewal failed"}

    # Phase 1: Standard upload attempts
    for attempt in range(1, MAX_RETRIES + 1):
        if global_state.interrupted:
            Console.group_end()
            return {"file": filename, "status": "skipped", "url": "", "error": "User interrupt"}
        
        Console.info(f"Uploading... (attempt {attempt}/{MAX_RETRIES})", indent=1)
        if uploader.upload_image(file_path):
            link = uploader.finalize_and_get_link()
            if link:
                Console.success(f"Uploaded: {link}", indent=1)
                uploader.processed_count += 1
                Console.live_countdown(BASE_WAIT_TIME, "Cooldown", indent=1)
                Console.group_end()
                return {"file": filename, "status": "success", "url": link, "error": ""}
        
        if attempt < MAX_RETRIES:
            wait_time = exponential_backoff(attempt)
            Console.warning(f"Failed, retrying in {wait_time}s...", indent=1)
            Console.live_countdown(wait_time, "Waiting", indent=1)
        else:
            Console.error(f"All attempts failed", indent=1)

    # Phase 2: Recovery mode (renew session and retry)
    Console.warning("Recovery mode: renewing session...", indent=1)
    if not uploader.initialize_session():
        Console.group_end()
        return {"file": filename, "status": "failed", "url": "", "error": "Session renewal failed"}

    for attempt in range(1, MAX_RETRIES + 1):
        if global_state.interrupted:
            Console.group_end()
            return {"file": filename, "status": "skipped", "url": "", "error": "User interrupt"}
        
        Console.info(f"Recovery upload... (attempt {attempt}/{MAX_RETRIES})", indent=1)
        if uploader.upload_image(file_path):
            link = uploader.finalize_and_get_link()
            if link:
                Console.success(f"Recovered! {link}", indent=1)
                uploader.processed_count += 1
                Console.live_countdown(BASE_WAIT_TIME, "Cooldown", indent=1)
                Console.group_end()
                return {"file": filename, "status": "success", "url": link, "error": ""}
        
        if attempt < MAX_RETRIES:
            wait_time = exponential_backoff(attempt)
            Console.live_countdown(wait_time, "Waiting", indent=1)
        else:
            Console.error(f"Recovery failed", indent=1)
    
    Console.error("Upload failed - skipping file", indent=1)
    Console.group_end()
    return {"file": filename, "status": "failed", "url": "", "error": "Max retries exceeded"}


def get_user_input_path() -> Optional[str]:
    """Prompts the user interactively if no path is configured."""
    Console.header("FILE PATH INPUT")
    print(f"{Fore.CYAN}Please provide the path to your images with following options and press 'Enter' to continue:")
    print(f"{Fore.YELLOW}• Enter a folder path containing images")
    print(f"{Fore.YELLOW}• Enter a single image file path")
    print(f"{Fore.YELLOW}• Drag and drop a folder or file into this window\n")
    
    try:
        user_input = input(f"{Fore.GREEN}Path: {Style.RESET_ALL}").strip()
        
        # Sanitize input (remove OS-added quotes)
        user_input = user_input.strip('"').strip("'")
        
        if not user_input:
            Console.error("No path provided")
            return None
        
        if not os.path.exists(user_input):
            Console.error(f"Path does not exist: {user_input}")
            return None
        
        return user_input
        
    except KeyboardInterrupt:
        print()
        Console.warning("Input cancelled by user")
        return None
    except Exception as e:
        Console.error(f"Error reading input: {e}")
        return None


def discover_files(directory: Optional[str] = None, cli_args: Optional[List[str]] = None) -> List[str]:
    """
    Locates images based on configuration, CLI args, or user input.
    Supports directory scanning and deduplication.
    """
    files = []
    
    # Check for interactive setup request
    if directory == 'set_file_path' or not directory:
        directory = get_user_input_path()
        if not directory:
            return []
    
    # Handle single file path
    if os.path.isfile(directory):
        Console.info(f"Single file mode: {directory}")
        return [directory]
    
    # Handle directory scan
    if os.path.isdir(directory):
        Console.info(f"Scanning directory: {directory}")
        extensions = ['*.jpg', '*.jpeg', '*.png', '*.gif']
        
        for ext in extensions:
            # Check both lowercase and uppercase extensions
            files.extend(glob.glob(os.path.join(directory, ext)))
            files.extend(glob.glob(os.path.join(directory, ext.upper())))
    
    # Handle CLI arguments if provided
    elif cli_args:
        Console.info("Using files from command line arguments")
        files = [f for f in cli_args if os.path.exists(f)]
    
    # Deduplicate and sort
    files = sorted(list(set(files)))
    
    return files


def save_csv_report(results: List[Dict], output_file: str, append: bool = False) -> bool:
    """Writes results to CSV, supporting both overwrite and append modes."""
    try:
        mode = 'a' if append and os.path.exists(output_file) else 'w'
        write_header = mode == 'w' or not os.path.exists(output_file)
        
        with open(output_file, mode, newline='', encoding='utf-8') as f:
            fieldnames = ['file', 'status', 'url', 'error']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            if write_header:
                writer.writeheader()
            
            writer.writerows(results)
        
        return True
        
    except PermissionError:
        Console.error(f"Permission denied: Cannot write to {output_file}")
        return False
    except Exception as e:
        Console.error(f"Failed to save CSV: {e}")
        return False


def print_summary(results: List[Dict], duration, total_files: int):
    """Generates the final execution report statistics."""
    success_count = sum(1 for r in results if r['status'] == 'success')
    fail_count = sum(1 for r in results if r['status'] == 'failed')
    skip_count = sum(1 for r in results if r['status'] == 'skipped')
    
    Console.header("SUMMARY")
    
    print(f"  {Fore.CYAN}Duration:      {Style.BRIGHT}{str(duration).split('.')[0]}")
    print(f"  {Fore.CYAN}Total:         {Style.BRIGHT}{total_files} files")
    print(f"  {Fore.GREEN}✔ Success:     {Style.BRIGHT}{success_count} {Fore.GREEN}({success_count/total_files*100:.1f}%)")
    print(f"  {Fore.RED}✖ Failed:      {Style.BRIGHT}{fail_count} {Fore.RED}({fail_count/total_files*100:.1f}%)")
    if skip_count > 0:
        print(f"  {Fore.YELLOW}⊝ Skipped:     {Style.BRIGHT}{skip_count} {Fore.YELLOW}({skip_count/total_files*100:.1f}%)")
    
    if fail_count > 0:
        print(f"\n  {Fore.RED}Failed files:")
        for r in results:
            if r['status'] == 'failed':
                error_msg = r.get('error', 'Unknown')
                print(f"    {Fore.RED}• {r['file']}: {error_msg}")
    
    print()


def main():
    """Main execution entry point."""
    Console.header("IMGBOX BULK UPLOADER -- by Frontliner")
    
    # Load state for resume functionality
    state = StateManager.load_state()
    completed_files = set(state.get('completed_files', []))
    all_results = state.get('results', [])
    
    if completed_files:
        Console.warning(f"Found previous session with {len(completed_files)} completed uploads")
        try:
            resume = input(f"  {Fore.YELLOW}Resume? (y/n): {Style.RESET_ALL}").strip().lower()
            if resume != 'y':
                completed_files = set()
                all_results = []
                StateManager.clear_state()
                Console.info("Starting fresh")
        except KeyboardInterrupt:
            print()
            Console.warning("Cancelled")
            return
    
    # Locate files
    files = discover_files(IMAGE_DIRECTORY, sys.argv[1:])
    
    if not files:
        Console.error("No files found")
        return

    # Filter already uploaded files
    remaining_files = [f for f in files if f not in completed_files]
    
    if not remaining_files:
        Console.success("All files already uploaded!")
        print_summary(all_results, datetime.now() - datetime.now(), len(files))
        return
    
    # Display configuration
    print(f"\n{Fore.CYAN}Config:")
    print(f"  Files: {len(files)} total | {len(completed_files)} done | {len(remaining_files)} remaining")
    print(f"  Limit: {MAX_FILE_SIZE_MB:.1f}MB ({SAFETY_MARGIN*100:.0f}% of {HOST_MAX_FILE_SIZE_MB}MB) | Formats: JPG, PNG, GIF")
    print(f"  Retry: {BASE_WAIT_TIME}s → {MAX_WAIT_TIME}s | Session: {SESSION_RENEW_LIMIT} uploads")
    
    # Instantiate uploader
    uploader = ImgboxUploader()
    global_state.uploader = uploader
    
    Console.group_start("SESSION SETUP")
    Console.info("Initializing session...", indent=1)
    if not uploader.initialize_session():
        Console.error("Failed to initialize", indent=1)
        Console.group_end()
        return
    Console.group_end()

    start_time = datetime.now()
    
    # Main Processing Loop
    try:
        for i, file_path in enumerate(remaining_files):
            if global_state.interrupted:
                Console.warning("\nInterrupted by user")
                break
            
            # Print separator line
            print()
            
            # Process single file
            result = process_file_robustly(uploader, file_path, i+1, len(remaining_files))
            all_results.append(result)
            completed_files.add(file_path)
            
            # Incremental save
            save_csv_report([result], CSV_OUTPUT_FILE, append=True)
            StateManager.save_state(list(completed_files), all_results)
    
    except Exception as e:
        Console.error(f"Unexpected error: {e}")
    
    finally:
        # Cleanup and Summary
        uploader.cleanup()
        duration = datetime.now() - start_time
        print_summary(all_results, duration, len(files))
        
        if len(completed_files) == len(files) and not global_state.interrupted:
            StateManager.clear_state()
            Console.success("✨ Complete! State cleared.")
        elif global_state.interrupted:
            Console.info(f"💾 Progress saved ({len(completed_files)}/{len(files)}). Run again to resume.")
        else:
            Console.info(f"💾 Done ({len(completed_files)}/{len(files)} processed).")


if __name__ == "__main__":
    main()
