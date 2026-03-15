// SPDX-License-Identifier: GPL-2.0
/*
 * inotify_trigger — inject inotify events without filesystem operations
 *
 * Exposes /proc/inotify_trigger.  Write to it to fire inotify events
 * on any path.  Calls the kernel's fsnotify() directly — no filesystem
 * operation occurs, no data touches the backing storage, and the event
 * is indistinguishable from a real VFS-generated one.
 *
 * Two formats:
 *
 *   1. Self-event (original):
 *        "<mask> <path>"
 *      Fires on the inode itself + its parent.
 *
 *   2. Directory + child name (for NFS / recursive watchers):
 *        "<mask> <dir_path>\t<child_name>"
 *      Resolves dir_path and fires "child_name changed in dir_path",
 *      which is the exact event inotify watchers on dir_path receive.
 *      child_name may contain slashes (e.g. "Albums/NewAlbum/song.flac").
 *      Only dir_path needs to exist — child_name is passed as the event
 *      name without any path resolution.
 *
 * Usage from userspace:
 *   # Self-events:
 *   echo "0x2 /media/music/song.flac"   > /proc/inotify_trigger   # IN_MODIFY
 *   echo "0x4 /media/music/SomeAlbum"   > /proc/inotify_trigger   # IN_ATTRIB
 *
 *   # Dir + child (preferred for NFS):
 *   printf "0x100 /media/music\tAlbums/new.flac"  > /proc/inotify_trigger  # IN_CREATE
 *   printf "0x200 /media/music\told.flac"          > /proc/inotify_trigger  # IN_DELETE
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
#include <linux/fsnotify.h>
#include <linux/fsnotify_backend.h>
#include <linux/slab.h>
#include <linux/version.h>

#define PROCFS_NAME  "inotify_trigger"
#define MAX_BUF      4096

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Inject inotify events without filesystem operations");
MODULE_VERSION("1.2");

static struct proc_dir_entry *proc_entry;

/*
 * Fire a self-event on an inode (original mode).
 *
 * Resolves @path_str to a dentry/inode, then fires using the two-phase
 * VFS pattern: fsnotify_parent() + fsnotify() self-event.
 */
static int fire_self_event(const char *path_str, __u32 mask)
{
	struct path path;
	struct dentry *dentry;
	struct inode *inode;
	int ret;

	ret = kern_path(path_str, LOOKUP_FOLLOW, &path);
	if (ret) {
		pr_debug("inotify_trigger: kern_path failed: %d for '%s'\n",
			 ret, path_str);
		return ret;
	}

	dentry = path.dentry;
	inode  = d_inode(dentry);

	if (S_ISDIR(inode->i_mode))
		mask |= FS_ISDIR;

	pr_debug("inotify_trigger: self-event 0x%x on ino %lu '%s'\n",
		 mask, inode->i_ino, path_str);

	fsnotify_parent(dentry, mask, dentry, FSNOTIFY_EVENT_DENTRY);
	fsnotify(mask, dentry, FSNOTIFY_EVENT_DENTRY, NULL, NULL, inode, 0);

	path_put(&path);
	return 0;
}

/*
 * Fire a directory + child event (NFS mode).
 *
 * Resolves @dir_path to a directory inode, then fires fsnotify() with
 * the child name — exactly what inotify watchers on the directory see
 * when VFS helpers like fsnotify_create()/fsnotify_modify() fire.
 *
 * Only @dir_path needs to exist.  @child_name is passed as the event
 * name without any path resolution, so it works for creates/deletes
 * where the child may or may not exist.
 */
static int fire_dir_event(const char *dir_path, const char *child_name,
			  __u32 mask)
{
	struct path path;
	struct inode *dir_inode;
	struct qstr qname;
	int ret;

	ret = kern_path(dir_path, LOOKUP_FOLLOW, &path);
	if (ret) {
		pr_debug("inotify_trigger: kern_path failed: %d for '%s'\n",
			 ret, dir_path);
		return ret;
	}

	dir_inode = d_inode(path.dentry);

	if (!S_ISDIR(dir_inode->i_mode)) {
		pr_debug("inotify_trigger: '%s' is not a directory\n",
			 dir_path);
		path_put(&path);
		return -ENOTDIR;
	}

	qname.hash = 0;
	qname.name = child_name;
	qname.len  = strlen(child_name);

	pr_debug("inotify_trigger: dir-event 0x%x dir='%s' child='%s'\n",
		 mask, dir_path, child_name);

	/*
	 * This is the same call pattern as fsnotify_dirent() which is
	 * used by fsnotify_create(), fsnotify_link(), etc.  It delivers
	 * the event to inotify watchers on the directory with child_name
	 * as the filename in the inotify_event structure.
	 */
	fsnotify(mask, path.dentry, FSNOTIFY_EVENT_DENTRY,
		 dir_inode, &qname, NULL, 0);

	path_put(&path);
	return 0;
}

/*
 * Parse one line.  Two formats:
 *   "<mask> <path>"              — self-event on the inode
 *   "<mask> <dir_path>\t<child>" — dir + child event (for NFS)
 */
static int process_line(char *line)
{
	char *path_str, *child_name;
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

	/* Check for tab separator: dir_path\tchild_name */
	child_name = strchr(path_str, '\t');
	if (child_name) {
		*child_name++ = '\0';
		if (!*child_name)
			return -EINVAL;
		return fire_dir_event(path_str, child_name, mask);
	}

	return fire_self_event(path_str, mask);
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
		if (ret) {
			pr_debug("inotify_trigger: process_line error: %d\n", ret);
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

	pr_info("inotify_trigger: /proc/%s ready (v1.2 dir+child mode)\n",
		PROCFS_NAME);
	return 0;
}

static void __exit inotify_trigger_exit(void)
{
	proc_remove(proc_entry);
	pr_info("inotify_trigger: /proc/%s removed\n", PROCFS_NAME);
}

module_init(inotify_trigger_init);
module_exit(inotify_trigger_exit);
