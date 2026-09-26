# 🎬 MKV Processing Pipeline

**Author:** ViperDavetheSnake (microbarley@gmail.com)

A Python 3.14 pipeline for processing and cleaning MKV files with comprehensive metadata extraction and optimal track selection. This project transforms raw downloaded movies into clean, Jellyfin-ready MKV files with proper metadata, optimal track selection, and file organization.

## 🎯 Overview

This pipeline consists of three main processing stages:

1. **Batch Cleaning** (`batch_cleaner.py`) - Downloads metadata from TMDB and applies clean tags to MKV files
2. **MKV Remuxing** (`mkv_remux_cleanroom.py`) - Creates optimized MKV files with selected tracks only
3. **Verification** (`verify_movies.py`) - Validates processed movies for Jellyfin compatibility

## 🏗️ Architecture

```
Raw Movies → [Batch Cleaner] → Tagged MKVs → [Remux Cleanroom] → Jellyfin-Ready → [Verification]
     ↓               ↓                ↓               ↓               ↓              ↓
  /movies/       TMDB Lookup       /tagged/       Track Selection   /cleaned/     ✅ Verified
                 NFO Creation                     Audio/Sub Opts
                 Poster Download                  Clean Output
```

## 📁 Directory Structure

```
movie_cleanup/
├── movies/                   # Raw downloaded movies (input)
├── tagged/                   # MKVs with TMDB metadata and tags
├── cleaned/                  # Final Jellyfin-ready MKV files
├── failed/                   # Failed processing attempts (incl. foreign-original films)
├── review/                   # Cases needing human review (multi-video, no acceptable audio)
├── logs/                     # Processing logs and debug info
└── helpers/                  # Utility scripts for debugging
    ├── peek_mkvinfo.py      # Quick MKV structure inspection
    ├── show_sample_tags.py  # Display generated tags XML
    └── scan_nfo_for_imdb.py # Scan NFO files for IMDb IDs
```

Foreign-original films (TMDB `original_language != 'en'`) are **not handled by
this pipeline** — batch_cleaner routes them to `./failed/`; they are managed by
separate scripts / manual processes.

## 🛠️ Scripts Overview

### Core Processing Scripts

#### `batch_cleaner.py` - TMDB Metadata Processor
- **Purpose**: Downloads metadata from TMDB and applies clean tags to MKV files
- **Workers**: 4 concurrent threads (storage-bound — see CLAUDE.md concurrency notes)
- **Features**:
  - TMDB API integration for rich metadata
  - Downloads high-quality movie posters
  - Strips existing tags and attachments
  - Renames files to clean format: `Movie_Title_(Year).mkv`
  - Generates complete NFO files for Jellyfin
  - Graceful shutdown handling

#### `mkv_remux_cleanroom.py` - Track Optimizer
- **Purpose**: Creates clean MKV files with optimal track selection
- **Workers**: 4 concurrent threads (storage-bound — see CLAUDE.md concurrency notes)
- **Features**:
  - Smart audio track selection (TrueHD → DTS-HD → DTS → EAC3 → AC3 → FLAC → AAC → MP3)
  - English-only audio filtering with commentary exclusion
  - Intelligent subtitle selection (SRT preferred, excludes SDH/forced)
  - Removes chapters and attachments for clean output
  - Preserves essential metadata files for Jellyfin

#### `verify_movies.py` - Jellyfin Compatibility Checker
- **Purpose**: Validates processed movies for Jellyfin readiness
- **Features**:
  - Comprehensive MKV structure analysis
  - NFO file validation (essential elements check)
  - Folder naming convention verification
  - Detailed reporting with pass/fail/warning status
  - JSON export for detailed analysis

### Utility Scripts (helpers/)

#### `peek_mkvinfo.py` - MKV Structure Inspector
- **Purpose**: Quick MKV structure inspection for debugging
- **Usage**: Shows first 30 lines of mkvinfo output for random samples

#### `show_sample_tags.py` - Tag XML Viewer
- **Purpose**: Displays generated tags XML for verification
- **Usage**: `python3 show_sample_tags.py [directory] [count]`

#### `scan_nfo_for_imdb.py` - IMDb ID Scanner
- **Purpose**: Scans NFO files for IMDb IDs and generates reports
- **Features**: CSV export and comprehensive folder analysis

## 🚀 Installation

### System Dependencies
```bash
# Ubuntu/Debian
sudo apt update
sudo apt install mkvtoolnix-cli wget

# Verify installation
mkvmerge --version
mkvpropedit --version
mkvinfo --version
```

### Python Dependencies
```bash
# This pipeline uses a small set of Python packages.
cd /home/david/git/radarr_cleanup
python3 -m pip install -r requirements.txt
```

### Multi-server Path Overrides
Use env vars when server paths differ:

```bash
export RC_SERVARR_DIR="/storage/media/servarr"    # stage dirs + logs (default $RC_MEDIA_BASE/servarr)
export RC_VERIFY_SCAN_DIR="/storage/media/movies" # library scanned by verify_movies.py
```

### Configuration
```bash
# Edit TMDB API key in batch_cleaner.py:
TMDB_API_KEY = 'your_api_key_here'

# Or set as environment variable:
export TMDB_API_KEY="your_tmdb_api_key_here"
```

**Note:** Get your TMDB API key from [The Movie Database](https://www.themoviedb.org/settings/api)

## 📖 Usage

### Complete Pipeline (Recommended)
```bash
# Step 1: Process raw movies with TMDB metadata
python3 batch_cleaner.py

# Step 2: Optimize tracks and create Jellyfin-ready files
python3 mkv_remux_cleanroom.py

# Step 3: Verify everything is Jellyfin-compatible
python3 verify_movies.py --scan-dir ./cleaned
```

### Individual Script Usage
```bash
# Batch processing
python3 batch_cleaner.py  # 4 workers, fixed

# Remuxing
python3 mkv_remux_cleanroom.py  # 4 workers, fixed

# Verification with different directory
python3 verify_movies.py --scan-dir /path/to/movies --threads 16

# Save verification results to JSON
python3 verify_movies.py --output results.json
```

### Utility Usage
```bash
# Debug and analysis tools
python3 helpers/peek_mkvinfo.py
python3 helpers/show_sample_tags.py ./tagged 5
python3 helpers/scan_nfo_for_imdb.py
```

## ⚙️ Configuration

### Audio Track Priority
The system selects audio tracks in this order:
1. **TrueHD** (highest quality)
2. **DTS-HD** (Master Audio / High Resolution)
3. **DTS**
4. **EAC3** (Dolby Digital Plus)
5. **AC3** (Dolby Digital)
6. **FLAC**
7. **AAC**
8. **MP3** (lowest priority)

### Track Filtering Rules
- **Audio**: English only, excludes commentary, director interviews
- **Subtitles**: English only, excludes SDH, forced, commentary tracks
- **Video**: Single video track required (multiple tracks moved to review)

### File Naming Convention
```
Movie_Title_(Year)_[2160p_hevc_eac3].mkv    inside    Movie_Title_(Year)/
```
- Special characters removed except spaces, hyphens, parentheses, ampersands
- Spaces replaced with underscores
- Technical suffix added at remux; the IMDb ID lives in the embedded MKV tags, not the filename

## 🔧 Advanced Features

### Concurrent Processing
- `batch_cleaner.py` and `mkv_remux_cleanroom.py`: **4 workers, fixed** — both
  stages are storage-bound against the shared ZFS pool; more workers make them
  slower, not faster (see CLAUDE.md for the full rationale and incident history)
- Graceful shutdown handling with signal management

### Error Handling
- **Non-critical failures**: Poster embedding failures don't break processing
- **Failed files**: Moved to `./failed/` directory for manual review
- **Review files**: Moved to `./review/` for cases requiring manual intervention
- **Comprehensive logging**: Detailed error messages with emoji indicators

### Metadata Extraction
Extracts and preserves:
- **Basic Info**: Title, year, director, writer, studio, plot
- **Identifiers**: IMDB, TMDB, TVDB IDs
- **Ratings**: IMDB, TMDB, Rotten Tomatoes scores
- **Media**: Poster URL, fanart URL, trailer URL
- **Actors**: Name, role, order, thumbnail
- **Classification**: Genres, countries, MPAA rating

## 🐛 Troubleshooting

### Common Issues

**"No movie.nfo found"**
- Ensure each movie folder contains a `movie.nfo` file
- Check file permissions and encoding

**"mkvmerge failed"**
- Verify MKVToolNix installation: `mkvmerge --version`
- Check MKV file integrity
- Ensure sufficient disk space

**"Multiple video tracks"**
- Files with multiple video tracks are moved to `./review/`
- Manual intervention required for these cases

**"No acceptable English audio"**
- Files without suitable English audio moved to `./review/`
- Check audio track language tags

### Debug Tools
```bash
# Check track structure
python3 test_mkv_tracks.py

# Verify tags generation
python3 show_sample_tags.py ./tagged

# Inspect MKV structure
python3 peek_mkvinfo.py
```

## 📊 Processing Status Indicators

- ✅ **Success**: File processed successfully
- ❌ **Error**: Processing failed, file moved to `./failed/`
- ⏳ **Processing**: Currently being processed
- ⏩ **Review**: Requires manual review, moved to `./review/`

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Test thoroughly with various MKV/NFO combinations
4. Ensure error handling is robust
5. Submit a pull request

## 📝 License

This project is open source. Feel free to modify and distribute as needed.

## 🆘 Support

For issues and questions:
- Check the troubleshooting section above
- Review error messages carefully
- Use the debug tools to analyze problematic files
- Ensure all system dependencies are properly installed

---

**Note**: This pipeline is designed for personal use with downloaded media. Ensure you have proper rights to process the media files you're working with.
