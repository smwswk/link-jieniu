const api = require('../../utils/api.js');

Page({
  data: {
    url: '',
    submitting: false,
    error: '',
    quota: { freeLeft: 1, extraUses: 0, isSubscribed: false },
    recentTasks: [],
  },

  onShow() {
    this.loadQuota();
    this.loadRecentTasks();
  },

  onUrlInput(e) {
    this.setData({ url: e.detail.value, error: '' });
  },

  loadQuota() {
    api.getUser().then(data => {
      this.setData({ quota: data });
    }).catch(() => {});
  },

  loadRecentTasks() {
    api.listTasks(1).then(data => {
      this.setData({ recentTasks: data.tasks || [] });
    }).catch(() => {});
  },

  onSubmit() {
    const url = this.data.url.trim();
    if (!url) {
      this.setData({ error: '请粘贴链接' });
      return;
    }
    if (!url.startsWith('http')) {
      this.setData({ error: '请粘贴有效链接（以 http 开头）' });
      return;
    }

    this.setData({ submitting: true, error: '' });

    api.createTask(url).then(data => {
      this.setData({ submitting: false, url: '' });
      wx.navigateTo({ url: '/pages/result/result?taskId=' + data.task_id });
      this.loadQuota();
      this.loadRecentTasks();
    }).catch(err => {
      this.setData({ submitting: false });
      if (err.statusCode === 402) {
        this.setData({ error: '今日免费次数已用完 前往"我的"页面兑换次数' });
      } else {
        this.setData({ error: err.message || '提交失败，请重试' });
      }
    });
  },

  onTapTask(e) {
    const taskId = e.currentTarget.dataset.id;
    wx.navigateTo({ url: '/pages/result/result?taskId=' + taskId });
  },
});
