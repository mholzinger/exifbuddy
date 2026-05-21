import datetime, exiftool, getopt, os, shutil, sys, time, json
from pathlib import Path
from datetime import date
from concurrent.futures import ThreadPoolExecutor
import multiprocessing


def generate_target_dictionary(metadata):
    '''
    Generates a dictionary containing metadata for files to be copied.

    Args:
        metadata (list): A list of dictionaries containing metadata for each file.

    Returns:
        dict: A dictionary where each key is the original filename, and the value is a dictionary
              containing the source path, destination folder, and formatted destination filename.
    '''
    copylist = {}

    for source_file in metadata:
      # Use filename and containing folder to derive new file name
      file =  str(Path(source_file['SourceFile']).name)
      fullpath = str(source_file['SourceFile'])
      print (file)
      containing_folder = str(Path(source_file['SourceFile']).parent.name)

      # Format our exif date and time
      # Try multiple possible date fields in order of preference
      date_fields = [
          'EXIF:DateTimeOriginal',  # When photo was taken
          'EXIF:CreateDate',         # When digital file was created
          'EXIF:ModifyDate',         # Last modified
          'File:FileModifyDate',     # File system modify date from exiftool
          'QuickTime:CreateDate',    # For video files
          'QuickTime:CreationDate',  # Alternative video date
      ]
      
      date_str = None
      for field in date_fields:
          if field in source_file:
              try:
                  # Handle different date formats
                  date_string = source_file[field]
                  # Some dates come with timezone info like '2025:10:18 12:34:56+02:00'
                  date_string = date_string.split('+')[0].split('-')[0].strip()
                  # Try to parse the date
                  date_obj = datetime.datetime.strptime(date_string, '%Y:%m:%d %H:%M:%S')
                  date_str = date_obj.strftime('%Y %b %d')
                  break
              except:
                  continue
      
      # Fall back to filesystem date if no EXIF date found
      if not date_str:
          created_time = os.path.getctime(fullpath)
          date_obj = datetime.datetime.fromtimestamp(created_time)
          date_str = date_obj.strftime('%Y %b %d')

      # Format new filename
      formatted_file = str(date_str) +' - ' + str(file)

      # Append to dictionary
      copylist[file] = {}
      copylist[file]['source'] = str(source_file['SourceFile'])
      copylist[file]['containing_folder'] = str(date_str) +' - ' + str(containing_folder)
      copylist[file]['dest_file'] = str(formatted_file)
      #print (copylist[file])

    return copylist


def search_files_in_path(search_path):
  '''
  Searches for files with a specific extension in a given directory path.

  Args:
      search_path (str): The directory path to search for files.

  Returns:
      list: A list of file paths matching the specified extension.
  '''
  suffix = ".jpg"
  discovered_files = []
  posix_path = Path(search_path)

  # Recursively iterate all items matching the glob pattern
  for glob_file in posix_path.rglob('*'):
    # Skip macOS AppleDouble metadata sidecars (e.g. ._DSC0001.JPG)
    if glob_file.name.startswith('._'):
      continue
    # .suffix property refers to .ext extension
    ext = glob_file.suffix
    # use the .lower() method to get lowercase version of extension
    if ext.lower() == suffix:
      discovered_files.append(str(glob_file))

  return discovered_files


def process_copylist(copylist, destination):
    '''
    Processes the copylist dictionary and copies files to the destination directory using threads.
    Uses a thread pool to limit concurrent operations and avoid "too many open files" errors.

    Args:
        copylist (dict): A dictionary containing file metadata for copying.
        destination (str): The destination directory where files will be copied.
    '''
    from concurrent.futures import ThreadPoolExecutor
    import multiprocessing
    
    # Limit threads to avoid "too many open files" error
    # Use CPU count * 4 or 32, whichever is smaller
    max_workers = min(multiprocessing.cpu_count() * 4, 32)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for items in copylist:
            future = executor.submit(write_new_files, copylist[items], destination)
            futures.append(future)
        
        # Wait for all tasks to complete
        for future in futures:
            try:
                future.result()
            except Exception as e:
                print(f"Error processing file: {e}")


def write_new_files(items, destination):
    '''
    Writes a single file to the destination directory, creating subdirectories as needed.

    Args:
        items (dict): A dictionary containing the source file path, destination folder, and filename.
        destination (str): The base destination directory.
    '''
    source = items['source']
    new_file = items['dest_file']
    subdir = items['containing_folder']

    # Fix path construction to avoid doubling
    output_path = Path(destination) / subdir
    target = output_path / new_file

    print ("writing '%s' to '%s'" %
      (str(new_file), str(output_path)))

    output_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, target)

# main
def main(argv):
    '''
    Main function to parse command-line arguments, search for files, extract metadata,
    and copy files to the destination directory.

    Args:
        argv (list): Command-line arguments passed to the script.
    '''
    if not argv:
        print("Usage: sort.py -i <search_path> -o <destination>")
        print("Options:")
        print("  -i, --ifile    Specify the input directory to search for image files")
        print("  -o, --ofile    Specify the output directory to copy and organize files")
        print("  -h             Display this help message")
        sys.exit(2)

    search_path = ''
    destination = ''
    try:
        opts, args = getopt.getopt(argv, "hi:o:", ["ifile=", "ofile="])
    except getopt.GetoptError:
        print('sort.py -i <search_path> -o <destination>')
        sys.exit(2)
    for opt, arg in opts:
        if opt == '-h':
            print('sort.py -i <search_path> -o <destination>')
            sys.exit()
        elif opt in ("-i", "--ifile"):
            search_path = arg
        elif opt in ("-o", "--ofile"):
            destination = arg
    print('Input path:', search_path)
    print('Output path:', destination)

    # Find files to process
    lines = search_files_in_path(search_path)

    # Get our metadata
    list_items = len(lines)
    print("Image files to process :", list_items)

    with exiftool.ExifToolHelper() as et:
        metadata = []
        # Process files in batches for better performance
        batch_size = 50
        for i in range(0, len(lines), batch_size):
            batch = lines[i:i+batch_size]
            try:
                # pyexiftool 0.5+: get_metadata accepts a list of files
                batch_metadata = et.get_metadata(batch)
                metadata.extend(batch_metadata)
                print(f"Processed batch {i//batch_size + 1} of {(len(lines)-1)//batch_size + 1}")
            except Exception as e:
                print(f"Error processing batch: {e}")
                # Fall back to individual processing for this batch
                for file in batch:
                    try:
                        file_metadata = et.get_metadata([file])[0]
                        metadata.append(file_metadata)
                    except Exception as e2:
                        print(f"Could not read metadata for {file}: {e2}")
                        # Add basic entry so file can still be processed
                        metadata.append({'SourceFile': file})

    if metadata:
        copylist = generate_target_dictionary(metadata)
        process_copylist(copylist, destination)
    else:
        print("No files to process!")


if __name__ == "__main__":
    main(sys.argv[1:])