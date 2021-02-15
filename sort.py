import os, shutil, exiftool, datetime
from datetime import date

# Define constants
file_list = ''
destination = ''
source_drive = ''

def copy_file_to_new_name(metadata):
  copylist = {}

  for source_file in metadata:
    # Use filename and containing foldeer to derive new file name
    file = os.path.basename(source_file['SourceFile'])
    path=os.path.dirname(source_file['SourceFile'])
    containing_folder = str(os.path.basename(path)).split('-')[-1]

    # Format our exif date and time
    date_obj = datetime.datetime.strptime(source_file['EXIF:CreateDate'], '%Y:%m:%d %H:%M:%S')
    date_str = date.strftime(date_obj, '%Y %b %d')

    # Format new filename
    formatted_file = str(date_str) +' -' + str(containing_folder) +'_' + str(file)

    # Append to dictionary
    copylist[file] = {}
    copylist[file]['source'] = str(source_file['SourceFile'])
    copylist[file]['dest_file'] = str(formatted_file)
    print (copylist[file])

  return copylist

# main
f = open(file_list)
lines = f.readlines()

# Get our list of files to copy
val = []
for line in lines:
  vfile = (source_drive + ''.join(line.replace('\n','')).split('.', 1)[-1])
  val.append(vfile)

# Get our metadata
with exiftool.ExifTool() as et:
  metadata = et.get_metadata_batch(val)

copylist = copy_file_to_new_name(metadata)

# Copy files from dictionary form source to new destination
for keys in copylist:
    source =  copylist[keys]['source']
    new_file = copylist[keys]['dest_file']
    target = destination + '/' + new_file

    #print ("writing \'%s\' to \'%s\'" % (str(new_file), str(destination)))
    #print ("fullpath \'{}\'".format(target))

    shutil.copy(source, destination)
