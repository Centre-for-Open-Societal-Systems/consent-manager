def call() {
pipeline {
    agent any
    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        // No retry: a repeated run of the same build number would find its own tags already published, and tags are never replaced.
        timestamps()
        timeout(time: 60, unit: 'MINUTES')
    }
    environment {
        AWS_REGION = 'ap-south-1'
    }
    stages {
        stage('Validate') {
            steps {
                // Discard files and build manifests left by an earlier run before reading this revision.
                deleteDir()
                checkout scm
                script {
                    // checkout scm does not always export GIT_COMMIT globally: resolve the workspace revision itself.
                    def commit = sh(returnStdout: true, script: 'git rev-parse --verify HEAD').trim()
                    if (!(commit ==~ /[0-9a-f]{40}/)) {
                        error('Could not resolve the checked-out Git commit')
                    }
                    env.SOURCE_COMMIT = commit
                    env.IMAGE_TAG = sh(returnStdout: true,
                        script: 'python3 ci/images/image_names.py tag "$BRANCH_NAME" "$BUILD_NUMBER"').trim()
                }
                // Static and mock tests only: no network, no AWS, no Docker.
                sh '''
                    set -eu
                    bash -n ci/images/images.sh
                    python3 -m unittest discover -s ci/tests -v
                '''
            }
        }
        stage('Build') {
            steps {
                sh 'bash ci/images/images.sh "$IMAGE_TAG" "$SOURCE_COMMIT"'
            }
        }
        stage('Publish') {
            // Pull requests and every branch but develop stop above this stage and never reach ECR. The script checks the
            // same conditions again and fails closed (ci/images/publish_images.py).
            when { allOf { branch 'develop'; not { changeRequest() } } }
            steps {
                // The credential exists only inside this block, as the standard AWS environment variables.
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds']]) {
                    sh '''
                        set +x
                        set -eu
                        mkdir -p service-outcomes
                        python3 ci/images/publish_images.py service-outcomes/consent-manager-images.json
                    '''
                }
            }
        }
    }
    post {
        always {
            archiveArtifacts artifacts: 'service-outcomes/*.json,service-outcomes/*.tsv', allowEmptyArchive: true
        }
    }
}
}
return this
