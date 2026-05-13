const api = require('../../utils/api.js');
const app = getApp();

Page({
  data: {
    loading: true,
    error: '',
    taskId: '',
    summary: null,
    cardUrl: '',
    markdownHtml: '',
    pollTimer: null,
  },

  statusTextMap: {
    pending: '排队中...',
    downloading: '正在下载音频...',
    transcribing: '正在语音转文字...',
    summarizing: 'AI 正在生成摘要...',
  },

  onLoad(options) {
    const taskId = options.taskId;
    this.setData({ taskId });
    if (taskId) {
      this.pollTask(taskId);
    }
  },

  onUnload() {
    if (this.data.pollTimer) {
      clearTimeout(this.data.pollTimer);
    }
  },

  pollTask(taskId) {
    api.getTask(taskId).then(data => {
      if (data.status === 'completed') {
        this.setData({
          loading: false,
          summary: data.summary,
          cardUrl: app.globalData.baseURL + data.summary.card_url,
          markdownHtml: this.simpleMarkdown(data.summary.full_text),
        });
      } else if (data.status === 'failed') {
        this.setData({
          loading: false,
          error: data.error_message || '处理失败，请重试',
        });
      } else {
        this.setData({
          statusText: this.statusTextMap[data.status] || '处理中...',
        });
        this.data.pollTimer = setTimeout(() => this.pollTask(taskId), 3000);
      }
    }).catch(err => {
      this.setData({
        loading: false,
        error: err.message || '获取任务状态失败',
      });
    });
  },

  simpleMarkdown(md) {
    let html = md
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      .replace(/\n/g, '<br/>');
    return html;
  },

  previewCard() {
    wx.previewImage({
      urls: [this.data.cardUrl],
      current: this.data.cardUrl,
    });
  },

  saveCard() {
    wx.showLoading({ title: '保存中...' });
    wx.downloadFile({
      url: this.data.cardUrl,
      success: (res) => {
        wx.saveImageToPhotosAlbum({
          filePath: res.tempFilePath,
          success: () => {
            wx.hideLoading();
            wx.showToast({ title: '已保存到相册', icon: 'success' });
          },
          fail: () => {
            wx.hideLoading();
            wx.showToast({ title: '保存失败，请授权相册权限', icon: 'none' });
          },
        });
      },
      fail: () => {
        wx.hideLoading();
        wx.showToast({ title: '下载失败', icon: 'none' });
      },
    });
  },

  goBack() {
    wx.navigateBack();
  },

  onShareAppMessage() {
    return {
      title: this.data.summary ? 'AI 替你读了《' + this.data.summary.title + '》' : '链接解牛 - 粘贴链接，秒懂长内容',
      path: '/pages/index/index',
      imageUrl: this.data.cardUrl || '',
    };
  },
});
