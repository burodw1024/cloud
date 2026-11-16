// Keep the full upstream build/checks; bound minifier workers on on-prem build hosts.
const config = require('./webpack.config');
const TerserPlugin = require('terser-webpack-plugin');
const CssMinimizerPlugin = require('css-minimizer-webpack-plugin');
config.optimization.minimizer = [
  new CssMinimizerPlugin({ parallel: 1 }),
  new TerserPlugin({ parallel: 1 }),
];
module.exports = config;
