import datetime, exiftool, getopt, shutil, sys
from pathlib import Path

def generate_target_dictionary(metadata):
  '''
  Some heredocs
  '''
  copylist = {}

  for source_file in metadata:
    # Use filename and containing foldeer to derive new file name
    file =  str(Path(source_file['SourceFile']).name)
    containing_folder = str(Path(source_file['SourceFile']).parent.name)

    # Format our exif date and time
    date_obj = datetime.datetime.strptime(source_file['EXIF:CreateDate'], '%Y:%m:%d %H:%M:%S')
    date_str = date.strftime(date_obj, '%Y %b %d')

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
  for keys in copylist:
    source =  copylist[keys]['source']
    new_file = copylist[keys]['dest_file']
    subdir = copylist[keys]['containing_folder']

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
  with exiftool.ExifTool() as et:
    metadata = et.get_metadata_batch(lines)

  copylist = generate_target_dictionary(metadata)
  #print (copylist)

  process_copylist(copylist, destination)


if __name__ == "__main__":
  main(sys.argv[1:])
