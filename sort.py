import datetime, exiftool, getopt, os, shutil, sys, time
from pathlib import Path
from datetime import date
import threading

def generate_target_dictionary(metadata):
  '''
  Some heredocs
  '''
  copylist = {}

  for source_file in metadata:
    # Use filename and containing foldeer to derive new file name
    file =  str(Path(source_file['SourceFile']).name)
    fullpath = str(source_file['SourceFile'])
    print (file)
    containing_folder = str(Path(source_file['SourceFile']).parent.name)

    # Format our exif date and time
    if 'EXIF:CreateDate' in source_file:
      date_obj = datetime.datetime.strptime(source_file['EXIF:CreateDate'], '%Y:%m:%d %H:%M:%S')
      date_str = date.strftime(date_obj, '%Y %b %d')
    else:
      # Todo: Format date string properly
      created_time = os.path.getctime(fullpath)
      year,month,day=time.localtime(created_time)[:3]
      date_str = ("%02d-%02d-%d"%(day,month,year))

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
  Some heredocs
  '''
  suffix = ".jpg"
  discovered_files = []
  posix_path = Path(search_path)

  # Recursively iterate all items matching the glob pattern
  for glob_file in posix_path.rglob('*'):
    # .suffix property refers to .ext extension
    ext = glob_file.suffix
    # use the .lower() method to get lowercase version of extension
    if ext.lower() == suffix:
      discovered_files.append(str(glob_file))

  return discovered_files

def process_copylist(copylist, destination):
  '''
  Copy files from dictionary source to output destination
  '''
  threads = []
  for items in copylist:
    thread = threading.Thread(target=write_new_files, args=(copylist[items],destination))
    threads.append(thread)
    thread.start()

    for thread in threads:
        thread.join()

def write_new_files(items, destination):
    source = items['source']
    new_file = items['dest_file']
    subdir = items['containing_folder']

    output_path = Path( destination + '/' + subdir )
    target = str(output_path) + '/' + new_file

    print ("writing \'%s\' to \'%s\'" %
      (str(new_file), (str(destination) + str(output_path))))
    #print ("fullpath \'{}\'".format(target))

    output_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, target)

# main
def main(argv):
  search_path = ''
  destination = ''
  try:
    opts, args = getopt.getopt(argv,"hi:o:",["ifile=","ofile="])
  except getopt.GetoptError:
    print ('sort.py -i <search_path> -o <destination>')
    sys.exit(2)
  for opt, arg in opts:
    if opt == '-h':
      print ('sort.py -i <search_path> -o <destination>')
      sys.exit()
    elif opt in ("-i", "--ifile"):
      search_path = arg
    elif opt in ("-o", "--ofile"):
      destination = arg
  print ('Input path:', search_path)
  print ('Output path:', destination)

  # Find files to process
  lines = search_files_in_path(search_path)

  # Get our metadata
  list_items = len(lines)
  print("Image files to process :", list_items)

  with exiftool.ExifTool() as et:
    metadata = et.get_metadata_batch(lines)

  copylist = generate_target_dictionary(metadata)
  #print (copylist)

  process_copylist(copylist, destination)


if __name__ == "__main__":
  main(sys.argv[1:])
