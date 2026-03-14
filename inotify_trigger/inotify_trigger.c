// SPDX-License-Identifier: GPL-2.0
/*
 * inotify_trigger — inject inotify events without filesystem operations
 *
 * Exposes /proc/inotify_trigger.  Write "<hex_mask> <path>" to fire an
 * inotify event on any path.  Calls the kernel's fsnotify() directly on
 * the resolved inode — no filesystem operation occurs, no data touches
 * the backing storage, and the event is indistinguishable from a real
 * VFS-generated one.
 *
 * Usage from userspace:
 *   echo "0x2 /media/music/song.flac"   > /proc/inotify_trigger   # IN_MODIFY
 *   echo "0x100 /media/music/new.flac"  > /proc/inotify_trigger   # IN_CREATE
 *   echo "0x200 /media/music/old.flac"  > /proc/inotify_trigger   # IN_DELETE
 *   echo "0x4 /media/music/SomeAlbum"   > /proc/inotify_trigger   # IN_ATTRIB
 *
 * Multiple events can be batched (one per line) in a single write().
 *
 * Requires: Linux >= 5.14 (for FSNOTIFY_EVENT_DENTRY)
 *           Module is GPL so it can call EXPORT_SYMBOL_GPL(fsnotify).
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/proc_fs.h>
#include <linux/uaccess.h>
#include <linux/namei.h>
#include <linux/fs.h>
#include <linux/fsnotify_backend.h>
#include <linux/slab.h>
#include <linux/version.h>

#define PROCFS_NAME  "inotify_trigger"
#define MAX_BUF      4096

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Inject inotify events without filesystem operations");
MODULE_VERSION("1.0");

static struct proc_dir_entry *proc_entry;

/*
 * Fire a single inotify event.
 *
 * Resolves @path_str to a dentry/inode via kern_path(), then calls
 * fsnotify() which delivers the event to every inotify (and fanotify)
 * watcher on that inode AND its parent directory.  No filesystem I/O.
 */
static int fire_event(const char *path_str, __u32 mask)
{
	struct path path;
	struct dentry *dentry;
	struct inode *inode;
	struct inode *dir_inode;
	int ret;

	ret = kern_path(path_str, LOOKUP_FOLLOW, &path);
	if (ret)
		return ret;

	dentry    = path.dentry;
	inode     = d_inode(dentry);
	dir_inode = d_inode(dentry->d_parent);

	/* Tag directories so fsnotify routes correctly */
	if (S_ISDIR(inode->i_mode))
		mask |= FS_ISDIR;

	/*
	 * This is the whole trick.  fsnotify() pushes the event into
	 * every fsnotify group (inotify, fanotify) that has a mark on
	 * either @inode or @dir_inode.  Passing dir_inode + d_name
	 * causes parent-directory watchers to see the event too (with
	 * FS_EVENT_ON_CHILD), exactly as a real VFS operation would.
	 *
	 * The backing filesystem is never touched.
	 */
	fsnotify(mask, dentry, FSNOTIFY_EVENT_DENTRY,
		 dir_inode, &dentry->d_name, inode, 0);

	path_put(&path);
	return 0;
}

/*
 * Parse one line: "<hex_or_dec_mask> <absolute_path>"
 */
static int process_line(char *line)
{
	char *path_str;
	unsigned int mask;
	int ret;

	/* Skip blank lines */
	if (!*line)
		return 0;

	path_str = strchr(line, ' ');
	if (!path_str)
		return -EINVAL;
	*path_str++ = '\0';

	/* Skip leading spaces in path */
	while (*path_str == ' ')
		path_str++;
	if (!*path_str)
		return -EINVAL;

	ret = kstrtouint(line, 0, &mask);
	if (ret)
		return ret;
	if (!mask)
		return -EINVAL;

	return fire_event(path_str, mask);
}

static ssize_t trigger_write(struct file *file, const char __user *ubuf,
			     size_t count, loff_t *ppos)
{
	char *buf, *line, *next;
	size_t len;
	int ret;

	if (count == 0)
		return 0;
	if (count > MAX_BUF)
		return -EINVAL;

	buf = kmalloc(count + 1, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	if (copy_from_user(buf, ubuf, count)) {
		kfree(buf);
		return -EFAULT;
	}
	buf[count] = '\0';

	/* Process each line (supports batched writes) */
	for (line = buf; line && *line; line = next) {
		next = strchr(line, '\n');
		if (next)
			*next++ = '\0';

		/* Strip trailing whitespace */
		len = strlen(line);
		while (len > 0 && (line[len - 1] == '\r' ||
				   line[len - 1] == ' '  ||
				   line[len - 1] == '\t'))
			line[--len] = '\0';

		ret = process_line(line);
		/*
		 * ENOENT is expected for deleted/moved files — the Python
		 * script handles this by falling back to the parent dir.
		 * All other errors are real failures.
		 */
		if (ret && ret != -ENOENT) {
			kfree(buf);
			return ret;
		}
	}

	kfree(buf);
	return count;
}

static const struct proc_ops trigger_ops = {
	.proc_write = trigger_write,
};

static int __init inotify_trigger_init(void)
{
	/* Mode 0200: write-only, root only */
	proc_entry = proc_create(PROCFS_NAME, 0200, NULL, &trigger_ops);
	if (!proc_entry)
		return -ENOMEM;

	pr_info("inotify_trigger: /proc/%s ready\n", PROCFS_NAME);
	return 0;
}

static void __exit inotify_trigger_exit(void)
{
	proc_remove(proc_entry);
	pr_info("inotify_trigger: /proc/%s removed\n", PROCFS_NAME);
}

module_init(inotify_trigger_init);
module_exit(inotify_trigger_exit);
