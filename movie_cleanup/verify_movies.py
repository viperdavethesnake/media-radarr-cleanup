#!/usr/bin/env python3
"""
🎬 Movie Verification Tool - Jellyfin Compatibility Checker

A modern Python 3.13+ tool for verifying movie collections are properly
processed and ready for Jellyfin media server.

Features:
- Concurrent verification with intelligent thread allocation
- Rich console output with colors and progress indicators
- Comprehensive MKV structure analysis
- NFO validation for Jellyfin compatibility
- JSON export capabilities
"""

import os
import subprocess
import json
import time
import traceback
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree.ElementTree import parse
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from pathlib import Path
import sys
from dotenv import load_dotenv

load_dotenv()

# ========== CONFIG =============
DEFAULT_SCAN_DIR = os.getenv('RC_VERIFY_SCAN_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'movies')
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
MAX_WORKERS = 12

# ========== MODERN STYLING =============
class Colors:
    """ANSI color codes for beautiful terminal output"""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    
    # Status colors
    SUCCESS = '\033[92m'  # Bright green
    WARNING = '\033[93m'  # Bright yellow
    ERROR = '\033[91m'    # Bright red
    INFO = '\033[94m'     # Bright blue
    
    # Accent colors
    CYAN = '\033[96m'     # Bright cyan
    MAGENTA = '\033[95m'  # Bright magenta
    WHITE = '\033[97m'    # Bright white
    GRAY = '\033[90m'     # Dark gray

@dataclass
class VerificationResult:
    """Modern data class for verification results."""
    folder: str
    path: str
    status: str
    issues: List[str]
    warnings: List[str]
    mkv_info: Optional[Dict[str, int]]
    metadata_info: Optional[Dict[str, List[str]]]
    folder_structure_ok: bool
    nfo_valid: bool
    
    def __post_init__(self):
        """Validate the result data"""
        if self.status not in {'PASSED', 'FAILED', 'WARNING', 'ERROR'}:
            self.status = 'UNKNOWN'

def log(msg: str) -> None:
    """Enhanced logging with timestamp"""
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}"
    print(line)
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    with open(Path(LOG_DIR) / 'verify_movies_debug.log', 'a', encoding='utf-8') as f:
        f.write(line + '\n')

def print_status(status: str, folder: str, details: str = "") -> None:
    """Print colorized status messages"""
    status_icons = {
        'PASSED': f"{Colors.SUCCESS}✅{Colors.RESET}",
        'FAILED': f"{Colors.ERROR}❌{Colors.RESET}",
        'WARNING': f"{Colors.WARNING}⚠️{Colors.RESET}",
        'ERROR': f"{Colors.ERROR}💥{Colors.RESET}",
        'PROCESSING': f"{Colors.INFO}🔍{Colors.RESET}",
    }
    
    icon = status_icons.get(status, "❓")
    color = getattr(Colors, status, Colors.RESET) if hasattr(Colors, status) else Colors.RESET
    
    # Truncate long folder names for better display
    display_folder = folder[:60] + "..." if len(folder) > 63 else folder
    
    print(f"{icon} {color}{display_folder}{Colors.RESET}", end="")
    if details:
        print(f" {Colors.DIM}({details}){Colors.RESET}")
    else:
        print()

def print_progress(current: int, total: int, start_time: float) -> None:
    """Print a beautiful progress bar"""
    if total == 0:
        return
        
    percent = (current / total) * 100
    elapsed = time.time() - start_time
    rate = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / rate if rate > 0 else 0
    
    # Progress bar
    bar_length = 30
    filled = int(bar_length * current // total)
    bar = "█" * filled + "░" * (bar_length - filled)
    
    print(f"\r{Colors.CYAN}Progress: {Colors.RESET}"
          f"{Colors.WHITE}[{bar}] {Colors.RESET}"
          f"{Colors.BOLD}{percent:5.1f}%{Colors.RESET} "
          f"{Colors.DIM}({current}/{total}) "
          f"Rate: {rate:.1f}/s "
          f"ETA: {eta/60:.1f}m{Colors.RESET}", end="", flush=True)

def find_main_mkv(folder):
    """Find the main MKV file in a folder"""
    for f in os.listdir(folder):
        if f.lower().endswith('.mkv'):
            return f
    return None

def check_mkv_structure(mkv_path):
    """Analyze MKV structure and return track information"""
    try:
        result = subprocess.run(['mkvmerge', '-J', mkv_path], 
                              capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None, f"mkvmerge failed: {result.stderr}"
        
        info = json.loads(result.stdout)
        tracks = info.get('tracks', [])
        
        # Count track types
        track_counts = {
            'video': len([t for t in tracks if t['type'] == 'video']),
            'audio': len([t for t in tracks if t['type'] == 'audio']),
            'subtitles': len([t for t in tracks if t['type'] == 'subtitles']),
            'attachments': len(info.get('attachments', [])),
            'chapters': len(info.get('chapters', [])),
            # mkvmerge -J reports global tags under 'global_tags' (not 'tags')
            'global_tags': len(info.get('global_tags', []))
        }
        
        # Check for specific issues
        issues = []
        
        # Multiple video tracks (should be 1)
        if track_counts['video'] != 1:
            issues.append(f"Expected 1 video track, found {track_counts['video']}")
        
        # Multiple audio tracks (should be 1 after remux)
        if track_counts['audio'] > 1:
            issues.append(f"Multiple audio tracks ({track_counts['audio']}) - may not be remuxed")
        
        # Multiple subtitle tracks (should be 0 or 1)
        if track_counts['subtitles'] > 1:
            issues.append(f"Multiple subtitle tracks ({track_counts['subtitles']}) - may not be remuxed")
        
        # Attachments (should be 0 after batch_cleaner)
        if track_counts['attachments'] > 0:
            issues.append(f"Found {track_counts['attachments']} attachments - not cleaned")
        
        # Chapters (remux runs mkvmerge with --no-chapters, so 0 expected)
        if track_counts['chapters'] > 0:
            issues.append(f"Found {track_counts['chapters']} chapter editions - not stripped by remux")

        # Global tags are the pipeline's source of truth (IMDb/TMDB identity
        # injected by batch_cleaner) — their ABSENCE is the defect.
        if track_counts['global_tags'] == 0:
            issues.append("No global tags - embedded IMDb/TMDB identity missing")
        
        # Check audio track language
        audio_tracks = [t for t in tracks if t['type'] == 'audio']
        if audio_tracks:
            audio_lang = audio_tracks[0]['properties'].get('language', '')
            if audio_lang and audio_lang.lower() not in ('en', 'eng', 'en-us', 'en-gb'):
                issues.append(f"Audio track language is '{audio_lang}', expected English")
        
        return track_counts, issues
        
    except subprocess.TimeoutExpired:
        return None, "mkvmerge timeout"
    except json.JSONDecodeError:
        return None, "Invalid JSON from mkvmerge"
    except Exception as e:
        return None, f"Error analyzing MKV: {e}"

def check_metadata_files(folder):
    """Check for required metadata files"""
    # After mkv_remux_cleanroom.py, only essential files should remain
    # Let Jellyfin handle additional artwork during library scans
    required_files = ['movie.nfo', 'poster.jpg']
    
    # These are intermediate files that get cleaned up during remuxing
    intermediate_files = ['metadata.json', 'tags.xml']
    
    missing_required = []
    present_files = []
    unexpected_files = []
    
    for file in required_files:
        if os.path.exists(os.path.join(folder, file)):
            present_files.append(file)
        else:
            missing_required.append(file)
    
    # Check for intermediate files that shouldn't be here after remuxing
    for file in intermediate_files:
        if os.path.exists(os.path.join(folder, file)):
            unexpected_files.append(file)
    
    return {
        'missing_required': missing_required,
        'unexpected_files': unexpected_files,
        'present_files': present_files
    }

def validate_nfo_file(nfo_path):
    """Validate movie.nfo file structure"""
    try:
        tree = parse(nfo_path)
        root = tree.getroot()
        
        # Check for essential elements that enable Jellyfin scraping
        essential_elements = ['title', 'year', 'imdbid', 'tmdbid']
        missing_essential = []
        
        for element in essential_elements:
            if root.find(element) is None:
                missing_essential.append(element)
        
        # Check if it's a valid movie NFO (not scene NFO)
        if root.tag != 'movie':
            return False, ["Root element is not 'movie' - likely a scene NFO"]
        
        # Check for basic content quality
        issues = []
        if missing_essential:
            issues.extend([f"Missing essential: {elem}" for elem in missing_essential])
        
        # Check if plot is too short (might be placeholder)
        plot_elem = root.find('plot')
        if plot_elem is not None and plot_elem.text:
            if len(plot_elem.text.strip()) < 20:
                issues.append("Plot seems too short (may be placeholder)")
        
        return len(missing_essential) == 0, issues
        
    except Exception as e:
        return False, [f"Error parsing NFO: {e}"]

def check_folder_structure(folder):
    """Check if folder name follows expected format"""
    folder_name = os.path.basename(folder)
    
    # Expected format: "Movie_Title_(Year)" — the pipeline uses underscores for
    # spaces (Jellyfin/Infuse compatibility). Space-separated names and an
    # optional trailing imdbid are accepted for pre-pipeline folders.
    import re

    pattern = r'^(.+?)[_ ]\((\d{4})\)(?:[_ ](tt\d{7,9}))?$'
    match = re.match(pattern, folder_name)

    if not match:
        return False, "Folder name doesn't match expected format: 'Movie_Title_(Year)'"
    
    title, year, imdbid = match.groups()
    
    # Basic validation
    if not title.strip():
        return False, "Empty title in folder name"
    
    if not (1900 <= int(year) <= 2030):
        return False, f"Year {year} seems invalid"
    
    return True, None

def verify_movie_folder(folder):
    """Verify a single movie folder"""
    base = os.path.basename(folder)
    log(f"\n🔍 Verifying: {base}")
    
    result = {
        'folder': base,
        'path': folder,
        'status': 'UNKNOWN',
        'issues': [],
        'warnings': [],
        'mkv_info': None,
        'metadata_info': None,
        'folder_structure_ok': False,
        'nfo_valid': False
    }
    
    try:
        # 1. Check folder structure
        folder_ok, folder_issue = check_folder_structure(folder)
        result['folder_structure_ok'] = folder_ok
        if not folder_ok:
            result['issues'].append(f"Folder structure: {folder_issue}")
        
        # 2. Check for MKV file
        mkv_file = find_main_mkv(folder)
        if not mkv_file:
            result['issues'].append("No MKV file found")
            result['status'] = 'FAILED'
            return result
        
        mkv_path = os.path.join(folder, mkv_file)
        
        # 3. Check MKV structure
        mkv_info, mkv_issues = check_mkv_structure(mkv_path)
        result['mkv_info'] = mkv_info
        
        if mkv_info is None:
            result['issues'].append(f"MKV analysis failed: {mkv_issues}")
            result['status'] = 'FAILED'
            return result
        
        if mkv_issues:
            result['issues'].extend(mkv_issues)
        
        # 4. Check metadata files
        metadata_info = check_metadata_files(folder)
        result['metadata_info'] = metadata_info
        
        if metadata_info['missing_required']:
            result['issues'].append(f"Missing required files: {', '.join(metadata_info['missing_required'])}")
        
        if metadata_info['unexpected_files']:
            result['warnings'].append(f"Unexpected intermediate files found: {', '.join(metadata_info['unexpected_files'])}")
        
        # 5. Validate NFO file
        nfo_path = os.path.join(folder, 'movie.nfo')
        if os.path.exists(nfo_path):
            nfo_valid, nfo_issues = validate_nfo_file(nfo_path)
            result['nfo_valid'] = nfo_valid
            if not nfo_valid:
                result['issues'].append(f"NFO validation failed: {', '.join(nfo_issues)}")
        else:
            result['issues'].append("movie.nfo file missing")
        
        # Determine overall status
        if result['issues']:
            result['status'] = 'FAILED'
        elif result['warnings']:
            result['status'] = 'WARNING'
        else:
            result['status'] = 'PASSED'
        
        log(f"  Status: {result['status']}")
        if result['issues']:
            log(f"  Issues: {len(result['issues'])}")
        if result['warnings']:
            log(f"  Warnings: {len(result['warnings'])}")
        
    except Exception as e:
        result['status'] = 'ERROR'
        result['issues'].append(f"Verification error: {e}")
        log(f"❌ Error verifying {base}: {e}")
    
    return result

def generate_summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate a beautiful, comprehensive summary report"""
    total = len(results)
    passed = len([r for r in results if r['status'] == 'PASSED'])
    failed = len([r for r in results if r['status'] == 'FAILED'])
    warnings = len([r for r in results if r['status'] == 'WARNING'])
    errors = len([r for r in results if r['status'] == 'ERROR'])
    
    success_rate = (passed / total) * 100 if total > 0 else 0
    
    # Beautiful header
    print(f"\n{Colors.BOLD}{Colors.WHITE}{'═' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}📊 VERIFICATION SUMMARY{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.WHITE}{'═' * 60}{Colors.RESET}")
    
    # Stats with colors
    print(f"{Colors.INFO}📁 Total movies scanned:{Colors.RESET} {Colors.BOLD}{total}{Colors.RESET}")
    print(f"{Colors.SUCCESS}✅ Passed:{Colors.RESET} {Colors.BOLD}{passed}{Colors.RESET}")
    print(f"{Colors.ERROR}❌ Failed:{Colors.RESET} {Colors.BOLD}{failed}{Colors.RESET}")
    print(f"{Colors.WARNING}⚠️  Warnings:{Colors.RESET} {Colors.BOLD}{warnings}{Colors.RESET}")
    print(f"{Colors.ERROR}💥 Errors:{Colors.RESET} {Colors.BOLD}{errors}{Colors.RESET}")
    
    # Success rate with color coding
    if success_rate >= 90:
        rate_color = Colors.SUCCESS
    elif success_rate >= 75:
        rate_color = Colors.WARNING
    else:
        rate_color = Colors.ERROR
        
    print(f"{Colors.INFO}🎯 Success rate:{Colors.RESET} {rate_color}{Colors.BOLD}{success_rate:.1f}%{Colors.RESET}")
    
    # Visual progress bar for success rate
    bar_length = 40
    filled = int(bar_length * success_rate / 100)
    bar = "█" * filled + "░" * (bar_length - filled)
    print(f"{Colors.INFO}📈 Progress:{Colors.RESET} {rate_color}[{bar}]{Colors.RESET}")
    
    print(f"{Colors.DIM}{'─' * 60}{Colors.RESET}")
    
    # Detailed breakdown
    if failed > 0:
        log(f"\n❌ FAILED MOVIES ({failed}):")
        for result in results:
            if result['status'] == 'FAILED':
                log(f"  • {result['folder']}")
                for issue in result['issues'][:3]:  # Show first 3 issues
                    log(f"    - {issue}")
                if len(result['issues']) > 3:
                    log(f"    - ... and {len(result['issues']) - 3} more issues")
    
    if warnings > 0:
        log(f"\n⚠️  MOVIES WITH WARNINGS ({warnings}):")
        for result in results:
            if result['status'] == 'WARNING':
                log(f"  • {result['folder']}")
                for warning in result['warnings']:
                    log(f"    - {warning}")
    
    # Common issues summary
    issue_counts = {}
    for result in results:
        for issue in result['issues']:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    
    if issue_counts:
        log(f"\n🔍 COMMON ISSUES:")
        for issue, count in sorted(issue_counts.items(), key=lambda x: x[1], reverse=True):
            log(f"  • {issue}: {count} movies")
    
    return {
        'total': total,
        'passed': passed,
        'failed': failed,
        'warnings': warnings,
        'errors': errors,
        'success_rate': (passed / total) * 100 if total > 0 else 0
    }

def main() -> int:
    """Main function with enhanced CLI and beautiful output"""
    parser = argparse.ArgumentParser(
        description='🎬 Verify movie folders are Jellyfin-ready',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
{Colors.BOLD}Examples:{Colors.RESET}
  {Colors.CYAN}python3 verify_movies.py{Colors.RESET}
    Verify default directory with 12 threads
    
  {Colors.CYAN}python3 verify_movies.py --scan-dir /movies --threads 16{Colors.RESET}
    Custom directory with 16 threads
    
  {Colors.CYAN}python3 verify_movies.py --output results.json{Colors.RESET}
    Save detailed results to JSON file

{Colors.BOLD}Status Indicators:{Colors.RESET}
  {Colors.SUCCESS}✅ PASSED{Colors.RESET}   - Movie is Jellyfin-ready
  {Colors.WARNING}⚠️ WARNING{Colors.RESET}  - Minor issues, but functional
  {Colors.ERROR}❌ FAILED{Colors.RESET}   - Requires attention
  {Colors.ERROR}💥 ERROR{Colors.RESET}    - Processing error occurred
"""
    )
    parser.add_argument('--scan-dir', default=DEFAULT_SCAN_DIR, 
                       help=f'Directory to scan for movies (default: {DEFAULT_SCAN_DIR})')
    parser.add_argument('--threads', type=int, default=MAX_WORKERS,
                       help=f'Number of worker threads (default: {MAX_WORKERS})')
    parser.add_argument('--output', help='Output detailed results to JSON file')
    parser.add_argument('--quiet', '-q', action='store_true', 
                       help='Reduce output verbosity')
    
    args = parser.parse_args()
    
    # Print beautiful header
    print(f"\n{Colors.BOLD}{Colors.MAGENTA}🎬 Movie Verification Tool{Colors.RESET}")
    print(f"{Colors.DIM}━{'━' * 50}{Colors.RESET}")
    print(f"{Colors.INFO}📁 Directory:{Colors.RESET} {args.scan_dir}")
    print(f"{Colors.INFO}🧵 Threads:{Colors.RESET}   {args.threads}")
    print(f"{Colors.INFO}📝 Output:{Colors.RESET}    {args.output or 'Console only'}")
    print(f"{Colors.DIM}━{'━' * 50}{Colors.RESET}\n")
    
    # Create log directory
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    
    if not Path(args.scan_dir).exists():
        print(f"{Colors.ERROR}❌ Error: Scan directory '{args.scan_dir}' does not exist{Colors.RESET}")
        return 1
    
    # Get all movie folders
    try:
        folders = [os.path.join(args.scan_dir, d) for d in os.listdir(args.scan_dir)
                  if os.path.isdir(os.path.join(args.scan_dir, d))]
        log(f"📂 Found {len(folders)} folders to verify")
    except Exception as e:
        log(f"❌ Error reading scan directory: {e}")
        return 1
    
    if not folders:
        log("ℹ️  No folders found to verify")
        return 0
    
    # Verify folders in parallel with beautiful progress display
    results = []
    start_time = time.time()
    completed_count = 0
    
    try:
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            futures = [pool.submit(verify_movie_folder, folder) for folder in folders]
            
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=300)  # 5 minute timeout per folder
                    results.append(result)
                    completed_count += 1
                    
                    # Update progress bar
                    if not args.quiet:
                        print_progress(completed_count, len(folders), start_time)
                    
                except Exception as e:
                    print(f"\n{Colors.ERROR}❌ Worker thread error: {e}{Colors.RESET}")
                    completed_count += 1
    
    except KeyboardInterrupt:
        print(f"\n{Colors.WARNING}⚠️  Verification interrupted by user{Colors.RESET}")
        return 1
    
    # Clear progress line
    if not args.quiet:
        print()  # New line after progress bar
    
    # Generate summary
    summary = generate_summary(results)
    
    # Save detailed results if requested
    if args.output:
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump({
                    'summary': summary,
                    'results': results,
                    'scan_directory': args.scan_dir,
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }, f, indent=2, ensure_ascii=False)
            log(f"💾 Detailed results saved to: {args.output}")
        except Exception as e:
            log(f"❌ Error saving results: {e}")
    
    # Return appropriate exit code
    if summary['failed'] > 0 or summary['errors'] > 0:
        log(f"\n❌ Verification completed with {summary['failed']} failures and {summary['errors']} errors")
        return 1
    elif summary['warnings'] > 0:
        log(f"\n⚠️  Verification completed with {summary['warnings']} warnings")
        return 0
    else:
        log(f"\n✅ All {summary['total']} movies passed verification!")
        return 0

if __name__ == "__main__":
    exit(main())
