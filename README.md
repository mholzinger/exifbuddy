# exifbuddy
Tools using python exiftool for managing images

# sort.py
This module processes a list of image files with metadata and creates a copy of the files in a specified output path with new image files labeled by:
- date [CCYY MM DD] + containing folder name + original filename

Example:

Input list:
```['/path-to-file/unsorted-project/DSC0123.jpg', '/unsorted-project/DSC0124.jpg', '/unsorted-project/DSC0125.jpg']```

Output:
```
$ ls sorted-project-files/
  2021 Feb 01 - unsorted-project_DSC0123.jpg
  2021 Feb 01 - unsorted-project_DSC0124.jpg
  2021 Feb 01 - unsorted-project_DSC0125.jpg
```

